"""Every active item's CLIP vector in memory, for its nearest neighbours (plan §6.3,
§6.4 "Lightbox & similarity walk").

The API process holds the vectors embedded with `CLIP_MODEL` as one float32 matrix,
a row per item, loaded on first use. The vectors are of unit length, so a matrix
product is the cosine similarity to every item at once.

The worker embeds in another process, so the API learns of new vectors from the
database: the newest finished `clip` job's id, one indexed lookup per query. When it
has changed, the matrix is read again, at most once per `RELOAD_MIN_INTERVAL_S`, so
a first backfill finishing a job a second doesn't reread it on every request. Only
the first load makes a request wait for it; a reload swaps the new matrix in, and
other requests use the old one meanwhile.
Items trashed or gone missing since the load are left out of the results.
"""

import logging
import threading
import time
from collections.abc import Callable

import numpy as np
import sqlalchemy as sa
from numpy.typing import NDArray
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.db.models import Item, ItemStatus, Job, JobStatus
from photo_triage.worker.layout_schedule import embedded_items
from photo_triage.worker.queue import Stage

logger = logging.getLogger(__name__)

RELOAD_MIN_INTERVAL_S = 10.0
"""How stale the matrix may be while the worker is embedding."""
LOAD_BATCH = 2048
"""Rows read from SQLite at a time while loading."""
SPARE_CANDIDATES = 16
"""Neighbours ranked beyond the limit, in case some have left the library."""


class Similarity:
    def __init__(
        self,
        engine: Engine,
        model: str,
        *,
        reload_min_interval_s: float = RELOAD_MIN_INTERVAL_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._model = model
        self._reload_min_interval_s = reload_min_interval_s
        self._monotonic = monotonic
        self._swap = threading.Lock()
        """Held while the matrix and its ids are replaced, so a reader gets a pair."""
        self._loading = threading.Lock()
        """Held while the matrix is read: one load at a time."""
        self._ids: NDArray[np.int64] = np.empty(0, np.int64)
        """The items, ascending, one per row of `_matrix`."""
        self._matrix: NDArray[np.float32] = np.empty((0, 0), np.float32)
        self._loaded_key: int | None = None
        """The newest done `clip` job when it was loaded; None before the first load."""
        self._loaded_at_s = 0.0

    def warm(self) -> None:
        """Load the matrix now, so the first lightbox doesn't wait for it. A failure
        is only logged: the first request tries again."""
        try:
            with Session(self._engine) as session:
                self._current(session)
        except Exception:
            logger.warning("Couldn't load the vectors for similarity yet", exc_info=True)

    @property
    def nbytes(self) -> int:
        """The memory the vectors take."""
        return self._matrix.nbytes + self._ids.nbytes

    def neighbours(self, session: Session, item_id: int, limit: int) -> list[int] | None:
        """Up to `limit` other active items, most similar to `item_id` first, or None
        if it has no vector."""
        ids, matrix = self._current(session)
        row = int(np.searchsorted(ids, item_id))
        if row >= len(ids) or ids[row] != item_id:
            return None
        similarity = matrix @ matrix[row]
        similarity[row] = -np.inf
        wanted = min(limit + SPARE_CANDIDATES, len(ids) - 1)
        if wanted <= 0:
            return []
        top = np.argpartition(-similarity, wanted - 1)[:wanted]
        ranked = [int(ids[i]) for i in top[np.argsort(-similarity[top], kind="stable")]]
        active = set(
            session.scalars(
                sa.select(Item.id).where(Item.id.in_(ranked), Item.status == ItemStatus.ACTIVE)
            )
        )
        return [i for i in ranked if i in active][:limit]

    def _current(self, session: Session) -> tuple[NDArray[np.int64], NDArray[np.float32]]:
        """The matrix, read again first if the worker has embedded since it was.

        Only the first load makes requests wait. After that, the request that finds
        the matrix stale reads it again, while any others go on with the old one."""
        key = session.scalar(
            sa.select(sa.func.max(Job.id)).where(
                Job.stage == Stage.CLIP, Job.status == JobStatus.DONE
            )
        )
        key = 0 if key is None else key
        if self._loaded_key is None:
            with self._loading:
                if self._loaded_key is None:
                    self._load(key)
        elif (
            key != self._loaded_key
            and self._monotonic() - self._loaded_at_s >= self._reload_min_interval_s
            and self._loading.acquire(blocking=False)
        ):
            try:
                self._load(key)
            finally:
                self._loading.release()
        with self._swap:
            return self._ids, self._matrix

    def _load(self, key: int) -> None:
        """Read every vector into a new matrix, then swap it in. The rows are
        streamed into a matrix made to size, so a reload holds little more than the
        old matrix and the new one: about 200 MB each for 100k items."""
        started_s = time.monotonic()
        query = embedded_items(self._model)
        # On a Core connection, in one read transaction so the count holds: the
        # ORM's per-row handling would be most of the time.
        with self._engine.connect() as connection, connection.begin():
            count = connection.scalar(sa.select(sa.func.count()).select_from(query.subquery()))
            count = count or 0
            ids = np.empty(count, np.int64)
            matrix = np.empty((0, 0), np.float32)
            result = connection.execution_options(yield_per=LOAD_BATCH).execute(query)
            for row, (item_id, blob) in enumerate(result):
                vector = np.frombuffer(blob, "<f4")
                if row == 0:
                    matrix = np.empty((count, len(vector)), np.float32)
                ids[row] = item_id
                matrix[row] = vector
        with self._swap:
            self._ids, self._matrix = ids, matrix
            self._loaded_key = key
            self._loaded_at_s = self._monotonic()
        logger.info(
            "Loaded %d %s vectors for similarity (%.0f MB) in %.1f s",
            len(ids),
            self._model,
            self.nbytes / 1e6,
            time.monotonic() - started_s,
        )
