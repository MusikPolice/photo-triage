"""The `thumbnail` stage over the tier-1 fixtures (plan §6.2): which items get
thumbnails, what queues and redraws them, and how the API serves them."""

import datetime as dt
import subprocess
from pathlib import Path

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from photo_triage.api.app import create_app
from photo_triage.db.models import Item, ItemFrame, Job, JobStatus
from photo_triage.files import ExifTool
from photo_triage.pipeline.frames import frame_path
from photo_triage.pipeline.thumbnails import MAP_SIZE, Thumbnail, draw, encode, thumbnail_path
from photo_triage.settings import Settings
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import JobQueue, RetryPolicy, Stage
from photo_triage.worker.stages import ItemStage, build_runners, item_stages

START = dt.datetime(2026, 11, 8, 2, tzinfo=dt.UTC)
SUPPORTED = 28
"""Tier-1 files the scanner takes: every one but the truncated JPEG and the PNG."""


class Pipeline:
    """Runs walks and every job they lead to, as the worker would."""

    def __init__(self, engine: Engine, settings: Settings, exiftool: ExifTool) -> None:
        self.engine = engine
        self.exiftool = exiftool
        self.now = START
        self.queue = JobQueue(self.clock, RetryPolicy(max_attempts=1))
        self.sessions = sessionmaker(engine)
        self.configure(settings)

    def configure(self, settings: Settings, **stages: ItemStage) -> None:
        self.settings = settings
        self.stages = {**item_stages(settings), **stages}
        self.worker = Worker(
            self.engine,
            build_runners(settings, self.engine, self.queue, self.clock, self.exiftool),
            queue=self.queue,
            clock=self.clock,
            item_stages=self.stages,
        )

    def clock(self) -> dt.datetime:
        return self.now

    def run(self) -> None:
        with self.sessions.begin() as session:
            self.queue.enqueue(session, Stage.WALK)
        self.worker.drain()

    def later(self) -> None:
        self.now += dt.timedelta(days=1)

    def items(self) -> dict[str, Item]:
        with self.sessions() as session:
            return {i.path: i for i in session.scalars(sa.select(Item))}

    def done(self, stage: Stage) -> dict[int, Job]:
        with self.sessions() as session:
            query = sa.select(Job).where(Job.stage == stage, Job.status == JobStatus.DONE)
            return {job.item_id: job for job in session.scalars(query) if job.item_id}

    def thumbnail(self, item_id: int, kind: Thumbnail = Thumbnail.LARGE) -> Path:
        return thumbnail_path(self.settings.derived_dir, item_id, kind)


@pytest.fixture
def pipeline(migrated: Engine, settings: Settings, exiftool: ExifTool) -> Pipeline:
    settings.photo_dir.mkdir(exist_ok=True)
    return Pipeline(migrated, settings, exiftool)


def _size(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image.load()
        return image.size


def _exiftool(path: Path, *args: str) -> None:
    subprocess.run(["exiftool", "-q", "-overwrite_original", *args, str(path)], check=True)


def test_every_supported_item_gets_both_thumbnails(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()

    items = {path: i for path, i in pipeline.items().items() if i.content_hash is not None}
    assert len(items) == SUPPORTED
    for path, item in items.items():
        assert item.width is not None and item.height is not None
        large = _size(pipeline.thumbnail(item.id))
        small = _size(pipeline.thumbnail(item.id, Thumbnail.MAP))
        assert max(large) == min(400, max(item.width, item.height)), path
        assert max(small) == MAP_SIZE, path
        # The item's shape, to within rounding.
        assert abs(large[0] / large[1] - item.width / item.height) < 0.02, path
        assert abs(small[0] / small[1] - item.width / item.height) < 0.05, path
    assert set(pipeline.done(Stage.THUMBNAIL)) == {i.id for i in items.values()}
    # The truncated JPEG fails its scan, so it never gets this far.
    with pipeline.sessions() as session:
        parked = session.scalars(sa.select(Job.stage).where(Job.status == JobStatus.PARKED))
        assert list(parked) == [Stage.SCAN]


def test_a_video_is_drawn_from_its_middle_frame(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    clip = pipeline.items()["video/clip.mov"]

    with pipeline.sessions() as session:
        count = session.scalar(sa.select(sa.func.count()).where(ItemFrame.item_id == clip.id))
    assert count == 5
    middle = frame_path(pipeline.settings.derived_dir, clip.id, 2)
    assert pipeline.thumbnail(clip.id).read_bytes() == encode(draw(middle, 400))


def test_after_processing_nothing_is_due(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()

    assert pipeline.worker.reconcile() == dict.fromkeys(pipeline.stages, 0)


def test_a_changed_file_is_drawn_again(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    scene = pipeline.items()["scenes/scene-01.jpg"]
    before = pipeline.thumbnail(scene.id).read_bytes()

    pipeline.later()
    (library / "scenes/scene-01.jpg").write_bytes((library / "scenes/scene-02.jpg").read_bytes())
    pipeline.run()

    assert pipeline.items()["scenes/scene-01.jpg"].id == scene.id
    assert pipeline.done(Stage.THUMBNAIL)[scene.id].finished_at == pipeline.now
    assert pipeline.thumbnail(scene.id).read_bytes() != before


@pytest.mark.parametrize(
    ("orientation", "size"),
    [(3, (400, 300)), (6, (300, 400))],  # 180°, which keeps the dimensions, and 90°
)
def test_turning_a_photo_alone_redraws_its_thumbnails_turned(
    pipeline: Pipeline, library: Path, orientation: int, size: tuple[int, int]
) -> None:
    pipeline.run()
    path = library / "scenes/scene-01.jpg"
    scene = pipeline.items()["scenes/scene-01.jpg"]
    before = pipeline.thumbnail(scene.id).read_bytes()
    pipeline.later()

    _exiftool(path, f"-Orientation#={orientation}")
    pipeline.run()

    turned = pipeline.items()["scenes/scene-01.jpg"]
    assert (turned.id, turned.content_hash) == (scene.id, scene.content_hash)
    assert turned.orientation == orientation
    redrawn = pipeline.thumbnail(scene.id)
    assert redrawn.read_bytes() != before
    assert redrawn.read_bytes() == encode(draw(path, 400))  # turned: see test_thumbnail_drawing.py
    assert _size(redrawn) == size


def test_a_redraw_giving_the_same_thumbnails_reports_no_change(
    pipeline: Pipeline, library: Path
) -> None:
    pipeline.run()
    drawn = pipeline.done(Stage.THUMBNAIL)
    mtimes = {i: pipeline.thumbnail(i).stat().st_mtime_ns for i in drawn}
    pipeline.later()

    bumped = ItemStage(
        code_version=2,
        upstream=(Stage.SCAN, Stage.METADATA_READ, Stage.FRAMES),
        settings_fingerprint="400px",
    )
    pipeline.configure(pipeline.settings, thumbnail=bumped)
    assert pipeline.worker.reconcile()[Stage.THUMBNAIL] == SUPPORTED
    pipeline.worker.drain()

    again = pipeline.done(Stage.THUMBNAIL)
    assert {i: j.stage_version for i, j in again.items()} == dict.fromkeys(drawn, "2+400px")
    assert {i: j.output_changed_at for i, j in again.items()} == dict.fromkeys(drawn, START)
    assert {i: pipeline.thumbnail(i).stat().st_mtime_ns for i in drawn} == mtimes


def test_a_new_thumbnail_size_redraws_every_item(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()
    pipeline.later()

    pipeline.configure(pipeline.settings.model_copy(update={"thumbnail_size": 256}))
    assert pipeline.worker.reconcile()[Stage.THUMBNAIL] == SUPPORTED
    pipeline.worker.drain()

    scene = pipeline.items()["scenes/scene-01.jpg"]
    assert _size(pipeline.thumbnail(scene.id)) == (256, 192)
    assert {j.stage_version for j in pipeline.done(Stage.THUMBNAIL).values()} == {"1+256px"}


def test_no_staging_file_is_left_behind(pipeline: Pipeline, library: Path) -> None:
    pipeline.run()

    root = pipeline.settings.derived_dir / "thumbnails"
    assert not list(root.rglob("*.tmp"))
    assert {p.parent.name for p in root.rglob("*.jpg")} == {"large", "map"}


# --- The API ---------------------------------------------------------------------------


@pytest.fixture
async def client(pipeline: Pipeline, library: Path) -> AsyncClient:
    pipeline.run()
    app = create_app(pipeline.settings)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.anyio
@pytest.mark.parametrize(("size", "kind"), [(400, Thumbnail.LARGE), (64, Thumbnail.MAP)])
async def test_the_api_serves_each_size_with_a_cache_header(
    client: AsyncClient, pipeline: Pipeline, size: int, kind: Thumbnail
) -> None:
    scene = pipeline.items()["scenes/scene-01.jpg"]

    response = await client.get(f"/api/items/{scene.id}/thumbnail", params={"size": size})

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert response.headers["cache-control"] == "private, no-cache"
    assert response.content == pipeline.thumbnail(scene.id, kind).read_bytes()
    etag = response.headers["etag"]
    again = await client.get(
        f"/api/items/{scene.id}/thumbnail",
        params={"size": size},
        headers={"If-None-Match": etag},
    )
    assert (again.status_code, again.content) == (304, b"")


@pytest.mark.anyio
async def test_a_redrawn_thumbnail_is_sent_again(client: AsyncClient, pipeline: Pipeline) -> None:
    scene = pipeline.items()["scenes/scene-01.jpg"]
    url = f"/api/items/{scene.id}/thumbnail?size=400"
    etag = (await client.get(url)).headers["etag"]

    pipeline.later()
    _exiftool(pipeline.settings.photo_dir / "scenes/scene-01.jpg", "-Orientation#=3")
    pipeline.run()

    response = await client.get(url, headers={"If-None-Match": etag})
    assert response.status_code == 200
    assert response.headers["etag"] != etag


@pytest.mark.anyio
@pytest.mark.parametrize("query", ["?size=400", "?size=64"])
async def test_an_unknown_item_has_no_thumbnail(client: AsyncClient, query: str) -> None:
    response = await client.get(f"/api/items/9999/thumbnail{query}")

    assert response.status_code == 404


@pytest.mark.anyio
async def test_only_the_two_sizes_are_served(client: AsyncClient, pipeline: Pipeline) -> None:
    scene = pipeline.items()["scenes/scene-01.jpg"]

    response = await client.get(f"/api/items/{scene.id}/thumbnail?size=128")

    assert response.status_code == 404
    assert response.json() == {"detail": "Thumbnails are 400 or 64 px"}


@pytest.mark.anyio
async def test_an_item_whose_thumbnail_is_missing_gets_a_404(
    client: AsyncClient, pipeline: Pipeline
) -> None:
    scene = pipeline.items()["scenes/scene-01.jpg"]
    pipeline.thumbnail(scene.id).unlink()

    response = await client.get(f"/api/items/{scene.id}/thumbnail?size=400")

    assert response.status_code == 404
