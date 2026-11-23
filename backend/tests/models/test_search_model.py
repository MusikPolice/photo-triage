"""Search with the real CLIP model (dev-environment §6, model tier): a text query
ranks the right tier-1 fixture first, above the floor, through the API's own text
encoder and matrix. `just test-models`."""

import datetime as dt
from pathlib import Path

import pillow_heif
import pytest
from PIL import Image
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.api.similarity import Similarity
from photo_triage.api.text_encoder import TextEncoder
from photo_triage.db.models import Embedding, Item, Job, JobStatus, MediaType
from photo_triage.ml import OnnxClip
from photo_triage.ml.clip import SEARCH_FLOOR
from photo_triage.pipeline.embed import to_blob
from photo_triage.settings import DEFAULT_MODELS_DIR, Settings
from photo_triage.worker.queue import PRIORITY, Stage

pytestmark = pytest.mark.models
pillow_heif.register_heif_opener()  # pyright: ignore[reportUnknownMemberType]

SYNTHETIC = Path(__file__).parents[1] / "fixtures" / "synthetic"
MODEL = "ViT-B-32"
NOW = dt.datetime(2026, 11, 23, tzinfo=dt.UTC)
PHOTOS = sorted(
    p.relative_to(SYNTHETIC).as_posix()
    for p in [*SYNTHETIC.rglob("*.jpg"), *SYNTHETIC.rglob("*.heic")]
    if p.parent.name != "broken"
)


def _open(name: str) -> Image.Image:
    with Image.open(SYNTHETIC / name) as image:
        image.load()
        return image.convert("RGB")


def test_a_text_query_ranks_the_right_fixture_first(migrated: Engine, settings: Settings) -> None:
    vectors = OnnxClip(DEFAULT_MODELS_DIR, MODEL, threads=4).embed_images(
        [_open(name) for name in PHOTOS]
    )
    names: dict[int, str] = {}
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
            names[item.id] = name
    encoder = TextEncoder(
        settings.model_copy(update={"models_dir": DEFAULT_MODELS_DIR, "clip_model": MODEL})
    )
    embedder = encoder.embedder()
    assert embedder.search_floor == SEARCH_FLOOR

    query = embedder.embed_texts(["a screenshot of text on a phone screen"])[0]
    with Session(migrated) as session:
        found = Similarity(migrated, MODEL).matches(session, query, embedder.search_floor)

    assert found, "nothing above the floor"
    assert names[found[0][0]] == "quality/screenshot.jpg", [(names[i], s) for i, s in found]
