"""The lightbox's neighbours with the real CLIP model (dev-environment §6, model
tier): each frame of a burst is nearest to another frame of it, among every
photo in the tier-1 fixtures. `just test-models`."""

import datetime as dt
from pathlib import Path

import pillow_heif
import pytest
from PIL import Image
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.api.similarity import Similarity
from photo_triage.db.models import Embedding, Item, Job, JobStatus, MediaType
from photo_triage.ml import OnnxClip
from photo_triage.pipeline.embed import to_blob
from photo_triage.settings import DEFAULT_MODELS_DIR
from photo_triage.worker.queue import PRIORITY, Stage

pytestmark = pytest.mark.models
pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
MODEL = "ViT-B-32"
NOW = dt.datetime(2026, 11, 21, tzinfo=dt.UTC)
PHOTOS = sorted(
    p.relative_to(SYNTHETIC).as_posix()
    for p in [*SYNTHETIC.rglob("*.jpg"), *SYNTHETIC.rglob("*.heic")]
    if p.parent.name != "broken"
)


def _open(name: str) -> Image.Image:
    with Image.open(SYNTHETIC / name) as image:
        image.load()
        return image.convert("RGB")


def test_a_burst_frame_s_nearest_neighbour_is_another_frame(migrated: Engine) -> None:
    clip = OnnxClip(DEFAULT_MODELS_DIR, MODEL, threads=4)
    vectors = clip.embed_images([_open(name) for name in PHOTOS])
    ids: dict[str, int] = {}
    with Session(migrated) as session, session.begin():
        for n, (name, vector) in enumerate(zip(PHOTOS, vectors, strict=True)):
            item = Item(
                content_hash=f"{n:064x}",
                path=name,
                media_type=MediaType.PHOTO,
                file_size_bytes=1,
                file_mtime_ns=0,
                first_seen_at=NOW,
                last_seen_at=NOW,
            )
            session.add(item)
            session.flush()
            session.add(Embedding(item_id=item.id, model=MODEL, vector=to_blob(vector)))
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
            ids[name] = item.id
    names = {item_id: name for name, item_id in ids.items()}
    similarity = Similarity(migrated, MODEL)

    burst = [name for name in PHOTOS if name.startswith("burst/")]
    assert len(burst) == 5
    with Session(migrated) as session:
        for frame in burst:
            nearest = similarity.neighbours(session, ids[frame], 1)
            assert nearest is not None
            assert names[nearest[0]] in set(burst) - {frame}, (frame, names[nearest[0]])
