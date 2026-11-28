"""The `quality` stage over the tier-1 fixtures, with the fake embedder (plan §6.8):
every item scored once, after CLIP, and scored again only when it's due. The
aesthetic score comes from a fake head, or stays null without the weights."""

import dataclasses
import logging
from collections.abc import Callable
from pathlib import Path

import numpy as np
import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from numpy.typing import NDArray
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from test_embeddings import START, SUPPORTED, Pipeline

from photo_triage.db.models import Embedding, Item, JobStatus, MediaType, Quality
from photo_triage.files import ExifTool
from photo_triage.ml import FakeClip
from photo_triage.ml.aesthetic import FakeHead, weights_path
from photo_triage.pipeline.embed import from_blob
from photo_triage.pipeline.quality import aesthetic_score
from photo_triage.settings import Settings
from photo_triage.worker.__main__ import main
from photo_triage.worker.queue import Stage
from photo_triage.worker.stages import ItemStage, item_stages


@pytest.fixture
def pipeline(migrated: Engine, settings: Settings, exiftool: ExifTool) -> Pipeline:
    settings.photo_dir.mkdir(exist_ok=True)
    return Pipeline(migrated, settings, exiftool)


def _quality(pipeline: Pipeline) -> dict[str, Quality]:
    with pipeline.sessions() as session:
        rows = session.execute(sa.select(Item.path, Quality).join(Quality))
        return {path: row for path, row in rows.tuples()}


def test_worker_once_scores_every_supported_item_once(
    migrated: Engine, settings: Settings, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("PHOTO_DIR", "TRASH_DIR", "DATA_DIR", "MODELS_DIR"):
        monkeypatch.setenv(name, str(getattr(settings, name.lower())))

    assert main(["scan"]) == 0
    assert main(["--once"], clip=FakeClip()) == 0

    with Session(migrated) as session:
        rows = list(session.scalars(sa.select(Quality)))
        hashed = set(session.scalars(sa.select(Item.id).where(Item.content_hash.is_not(None))))
    assert len(hashed) == SUPPORTED
    assert sorted(r.item_id for r in rows) == sorted(hashed)  # exactly one each
    for row in rows:
        for value in (row.blur, row.exposure, row.resolution, row.mean_brightness):
            assert 0.0 <= value <= 1.0
        assert (row.aesthetic, row.junk_label, row.junk_score) == (None, None, None)


def test_each_item_is_scored_after_its_clip_job(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    clip, quality = pipeline.jobs(Stage.CLIP), pipeline.jobs(Stage.QUALITY)
    assert set(quality) == set(clip)
    for item_id, job in quality.items():
        clipped_at = clip[item_id].finished_at
        assert job.started_at is not None and clipped_at is not None
        assert job.started_at >= clipped_at
        assert job.id > clip[item_id].id


def test_a_finished_clip_job_queues_a_quality_job(pipeline: Pipeline, library: Path) -> None:
    pipeline.configure(pipeline.settings, without=Stage.QUALITY)
    pipeline.run()
    assert set(pipeline.jobs(Stage.QUALITY, JobStatus.PENDING)) == set(pipeline.jobs(Stage.CLIP))
    assert _quality(pipeline) == {}


def test_the_poor_fixtures_score_low_where_they_should(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    rows = _quality(pipeline)
    scene = rows["scenes/scene-01.jpg"]
    assert rows["quality/blurred.jpg"].blur < scene.blur
    assert rows["quality/overexposed.jpg"].exposure < scene.exposure
    assert rows["quality/underexposed.jpg"].exposure < scene.exposure
    assert "broken/damaged.jpg" in rows  # scored from what decodes


def test_a_video_is_scored_from_its_frames(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    videos = [i for i in pipeline.items().values() if i.media_type == MediaType.VIDEO]
    rows = _quality(pipeline)
    assert videos
    for video in videos:
        assert video.path in rows


def test_bumping_its_version_scores_every_item_again(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    pipeline.later()
    stage = item_stages(pipeline.settings)[Stage.QUALITY]
    bumped = dataclasses.replace(stage, code_version=stage.code_version + 1)

    pipeline.configure(pipeline.settings, quality=bumped)
    assert pipeline.worker.reconcile() == {
        **dict.fromkeys(pipeline.stages, 0),
        Stage.QUALITY: SUPPORTED,
    }
    pipeline.worker.drain()

    jobs = pipeline.jobs(Stage.QUALITY)
    assert {j.stage_version for j in jobs.values()} == {bumped.version}
    # The same pictures give the same signals, so nothing downstream need run.
    assert {j.output_changed_at for j in jobs.values()} == {START}


def test_an_item_scored_at_the_current_version_isn_t_queued_again(
    pipeline: Pipeline, library: Path
) -> None:
    pipeline.run()
    pipeline.later()
    assert pipeline.worker.reconcile()[Stage.QUALITY] == 0


def test_a_changed_file_is_scored_again(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    pipeline.later()
    (library / "scenes/scene-01.jpg").write_bytes((library / "quality/blurred.jpg").read_bytes())
    pipeline.run()

    rows = _quality(pipeline)
    assert rows["scenes/scene-01.jpg"].blur == rows["quality/blurred.jpg"].blur
    jobs, items = pipeline.jobs(Stage.QUALITY), pipeline.items()
    assert jobs[items["scenes/scene-01.jpg"].id].output_changed_at == pipeline.now
    assert jobs[items["scenes/scene-02.jpg"].id].output_changed_at == START


@pytest.mark.anyio
async def test_the_activity_page_counts_the_quality_stage(
    pipeline: Pipeline, library: Path, client: AsyncClient
) -> None:
    pipeline.run()
    response = await client.get("/api/activity")
    rows = {row["stage"]: row for row in response.json()["stages"]}
    assert rows["quality"]["kind"] == "item"
    assert (rows["quality"]["done"], rows["quality"]["total"]) == (SUPPORTED, SUPPORTED)


def test_the_aesthetic_score_is_the_head_s_rating_mapped_to_0_to_1(
    pipeline: Pipeline, library: Path
) -> None:
    head = FakeHead()
    pipeline.configure(pipeline.settings, _clip(pipeline), aesthetic=head)
    pipeline.run()

    with pipeline.sessions() as session:
        vectors = {e.item_id: from_blob(e.vector) for e in session.scalars(sa.select(Embedding))}
        rows = list(session.scalars(sa.select(Quality)))
    assert len(rows) == SUPPORTED
    scores = {row.item_id: row.aesthetic for row in rows}
    for item_id, aesthetic in scores.items():
        assert aesthetic == aesthetic_score(head.rating(vectors[item_id]))
    assert len(set(scores.values())) > 1  # different items, different ratings


def test_without_the_weights_aesthetic_stays_null_and_the_log_says_where_they_go(
    pipeline: Pipeline, library: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, "photo_triage.worker.stages"):
        pipeline.run()

    rows = _quality(pipeline)
    assert len(rows) == SUPPORTED
    assert all(row.aesthetic is None for row in rows.values())
    assert all(row.blur is not None and row.exposure is not None for row in rows.values())
    expected = weights_path(pipeline.settings.models_dir, pipeline.settings.clip_model)
    warnings = [r.getMessage() for r in caplog.records if "aesthetic" in r.getMessage()]
    assert len(warnings) == 1  # once per worker, not once per item
    assert str(expected) in warnings[0]


def test_an_embedding_from_another_model_gets_no_aesthetic_score(
    pipeline: Pipeline, library: Path
) -> None:
    pipeline.configure(pipeline.settings, _clip(pipeline), aesthetic=FakeHead())
    pipeline.run()
    assert all(row.aesthetic is not None for row in _quality(pipeline).values())
    with pipeline.sessions.begin() as session:
        session.execute(sa.update(Embedding).values(model="ViT-B-16"))
    pipeline.later()
    pipeline.configure(
        pipeline.settings, _clip(pipeline), aesthetic=FakeHead(), quality=_bumped(pipeline)
    )
    pipeline.worker.reconcile()
    pipeline.worker.drain()

    assert all(row.aesthetic is None for row in _quality(pipeline).values())


def test_putting_the_weights_in_place_scores_the_library_again(
    pipeline: Pipeline,
    library: Path,
    write_linear_pth: Callable[[Path, NDArray[np.float32], float], None],
) -> None:
    pipeline.configure(pipeline.settings, _clip(pipeline))
    pipeline.run()
    assert all(row.aesthetic is None for row in _quality(pipeline).values())
    pipeline.later()
    path = weights_path(pipeline.settings.models_dir, pipeline.settings.clip_model)
    assert path is not None
    head = FakeHead()
    write_linear_pth(path, head.weight, head.bias)

    pipeline.configure(pipeline.settings, _clip(pipeline))
    assert pipeline.worker.reconcile()[Stage.QUALITY] == SUPPORTED
    pipeline.worker.drain()
    assert all(row.aesthetic is not None for row in _quality(pipeline).values())


def test_under_worker_fake_clip_the_fake_head_scores_each_item(
    migrated: Engine, settings: Settings, library: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("PHOTO_DIR", "TRASH_DIR", "DATA_DIR", "MODELS_DIR"):
        monkeypatch.setenv(name, str(getattr(settings, name.lower())))
    monkeypatch.setenv("WORKER_FAKE_CLIP", "true")

    assert main(["scan"]) == 0
    assert main(["--once"]) == 0

    with Session(migrated) as session:
        vectors = {e.item_id: from_blob(e.vector) for e in session.scalars(sa.select(Embedding))}
        rows = list(session.scalars(sa.select(Quality)))
    assert len(rows) == SUPPORTED
    for row in rows:
        assert row.aesthetic == aesthetic_score(FakeHead().rating(vectors[row.item_id]))


def _clip(pipeline: Pipeline) -> FakeClip:
    """The fake embedder under `CLIP_MODEL`'s name, as under `WORKER_FAKE_CLIP`, so
    the head rates its vectors."""
    return FakeClip(pipeline.settings.clip_model)


def _bumped(pipeline: Pipeline) -> ItemStage:
    stage = item_stages(pipeline.settings)[Stage.QUALITY]
    return dataclasses.replace(stage, code_version=stage.code_version + 1)
