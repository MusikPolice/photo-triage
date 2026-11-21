"""The `atlases` stage and its API: the map's thumbnails packed into sheets (plan §6.2)."""

import datetime as dt
from pathlib import Path

import numpy as np
import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from photo_triage.api.app import create_app
from photo_triage.db.models import Item, ItemStatus, Job, JobStatus, Layout, MediaType
from photo_triage.ml import FakeClip
from photo_triage.pipeline import atlases
from photo_triage.pipeline.thumbnails import Thumbnail, thumbnail_path
from photo_triage.settings import Settings
from photo_triage.worker import atlas_schedule
from photo_triage.worker.__main__ import main
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import PRIORITY, JobQueue, Stage
from photo_triage.worker.stages import Runner

START = dt.datetime(2026, 11, 18, 2, tzinfo=dt.UTC)


class Library:
    """Laid-out items with 64 px thumbnails, written straight to the database and
    the derived folder."""

    def __init__(self, engine: Engine, derived_dir: Path) -> None:
        self.sessions = sessionmaker(engine)
        self.derived_dir = derived_dir
        self.queue = JobQueue(lambda: START)
        self._next = 0

    def add(
        self,
        count: int,
        *,
        version: int = 1,
        size: tuple[int, int] = (64, 48),
        thumbnail: bool = True,
    ) -> list[int]:
        ids: list[int] = []
        rng = np.random.default_rng(self._next)
        with self.sessions.begin() as session:
            for _ in range(count):
                self._next += 1
                item = Item(
                    content_hash=f"{self._next:064x}",
                    path=f"photos/{self._next}.jpg",
                    media_type=MediaType.PHOTO,
                    file_size_bytes=1,
                    file_mtime_ns=0,
                    first_seen_at=START,
                    last_seen_at=START,
                )
                session.add(item)
                session.flush()
                x, y = rng.uniform(size=2)
                session.add(Layout(layout_version=version, item_id=item.id, x=x, y=y))
                ids.append(item.id)
        if thumbnail:
            for item_id in ids:
                self.thumbnail(item_id, size)
        return ids

    def thumbnail(self, item_id: int, size: tuple[int, int]) -> Path:
        path = thumbnail_path(self.derived_dir, item_id, Thumbnail.MAP)
        path.parent.mkdir(parents=True, exist_ok=True)
        shade = (item_id * 37) % 256
        Image.new("RGB", size, (shade, 255 - shade, 128)).save(path, quality=95)
        return path

    def build(self) -> atlases.Built:
        return atlases.build(self.sessions, self.derived_dir)

    def manifest(self, version: int) -> atlases.Manifest:
        path = atlas_schedule.set_path(self.derived_dir, version) / atlas_schedule.MANIFEST
        return atlases.decode_manifest(path.read_bytes())

    def sets(self) -> list[str]:
        root = atlas_schedule.atlas_root(self.derived_dir)
        return sorted(p.name for p in root.iterdir()) if root.exists() else []

    def queue_if_due(self) -> bool:
        with self.sessions.begin() as session:
            return atlas_schedule.queue_if_due(session, self.queue, self.derived_dir)

    def atlases_jobs(self) -> list[JobStatus]:
        with self.sessions() as session:
            return list(session.scalars(sa.select(Job.status).where(Job.stage == Stage.ATLASES)))


@pytest.fixture
def lib(migrated: Engine, settings: Settings) -> Library:
    return Library(migrated, settings.derived_dir)


# The curve and the manifest


def test_the_hilbert_curve_visits_each_cell_once_and_moves_one_step_at_a_time() -> None:
    xs, ys = np.meshgrid(np.arange(8), np.arange(8))
    x, y = xs.ravel().astype(np.int64), ys.ravel().astype(np.int64)
    d = atlases.hilbert_index(x, y, 3)

    assert sorted(d.tolist()) == list(range(64))
    walk = np.argsort(d)
    steps = np.abs(np.diff(x[walk])) + np.abs(np.diff(y[walk]))
    assert set(steps.tolist()) == {1}


def test_a_cell_round_trips_through_the_manifest() -> None:
    cells = [
        atlases.Cell(item_id=9, sheet=1, cell=4095, width=48, height=64),
        atlases.Cell(item_id=2, sheet=0, cell=65, width=64, height=36),
    ]
    data = atlases.encode_manifest(atlases.Manifest(version=7, sheets=2, cells=cells))

    decoded = atlases.decode_manifest(data)

    assert (decoded.version, decoded.sheets) == (7, 2)
    assert decoded.cells == sorted(cells, key=lambda c: c.item_id)
    assert len(data) == 20 + 10 * len(cells)


def test_a_thumbnail_that_isnt_square_is_centred_in_its_cell() -> None:
    # Cell 65 is row 1, column 1.
    assert atlases.Cell(1, 0, 65, width=64, height=48).origin == (64, 72)
    assert atlases.Cell(1, 0, 65, width=36, height=64).origin == (78, 64)


# Building a set


def test_each_laid_out_item_gets_a_cell_holding_its_thumbnail(lib: Library) -> None:
    wide = lib.add(5, size=(64, 48))
    tall = lib.add(5, size=(36, 64))

    built = lib.build()

    assert (built.version, built.items, built.sheets) == (1, 10, 1)
    manifest = lib.manifest(1)
    assert [c.item_id for c in manifest.cells] == wide + tall
    with Image.open(atlas_schedule.set_path(lib.derived_dir, 1) / "0.jpg") as sheet:
        assert sheet.size == (4096, 4096)
        for cell in manifest.cells:
            expected = (48, 64) if cell.item_id in wide else (64, 36)
            assert (cell.height, cell.width) == expected
            _assert_matches(
                sheet, cell, thumbnail_path(lib.derived_dir, cell.item_id, Thumbnail.MAP)
            )


def test_4097_items_take_two_sheets(lib: Library) -> None:
    lib.add(4097, size=(8, 8))

    built = lib.build()

    assert (built.items, built.sheets) == (4097, 2)
    folder = atlas_schedule.set_path(lib.derived_dir, 1)
    assert sorted(p.name for p in folder.iterdir()) == ["0.jpg", "1.jpg", "manifest.bin"]
    sheets = [c.sheet for c in lib.manifest(1).cells]
    assert (sheets.count(0), sheets.count(1)) == (4096, 1)


def test_neighbours_on_the_map_share_a_sheet(lib: Library) -> None:
    ids = lib.add(4096 * 2, size=(4, 4))
    rng = np.random.default_rng(3)
    left, right = ids[::2], ids[1::2]
    with lib.sessions.begin() as session:
        for half, low in ((left, 0.0), (right, 0.5)):
            for item_id in half:
                x, y = low + rng.uniform(0, 0.5), rng.uniform(0, 1)
                session.execute(sa.update(Layout).where(Layout.item_id == item_id).values(x=x, y=y))

    lib.build()

    # Two sheets along a Hilbert curve split the square into halves, left and right.
    sheet = {c.item_id: c.sheet for c in lib.manifest(1).cells}
    assert {sheet[i] for i in left} == {0}
    assert {sheet[i] for i in right} == {1}


def test_an_item_without_a_thumbnail_gets_no_cell(lib: Library) -> None:
    drawn = lib.add(3)
    lib.add(2, thumbnail=False)

    lib.build()

    assert [c.item_id for c in lib.manifest(1).cells] == drawn


def test_items_no_longer_active_get_no_cell(lib: Library) -> None:
    ids = lib.add(3)
    with lib.sessions.begin() as session:
        session.get_one(Item, ids[0]).status = ItemStatus.TRASHED

    lib.build()

    assert [c.item_id for c in lib.manifest(1).cells] == ids[1:]


def test_a_new_layout_replaces_the_old_set(lib: Library) -> None:
    lib.add(3, version=1)
    lib.build()
    with lib.sessions.begin() as session:
        session.execute(sa.update(Layout).values(layout_version=2))

    lib.build()

    assert lib.sets() == ["2"]


def test_no_layout_removes_every_set_and_any_staging_left(lib: Library) -> None:
    lib.add(3)
    lib.build()
    (atlas_schedule.atlas_root(lib.derived_dir) / ".staging-crashed").mkdir()
    with lib.sessions.begin() as session:
        session.execute(sa.delete(Layout))

    built = lib.build()

    assert built.version is None
    assert lib.sets() == []


def test_a_crashed_build_leaves_the_old_set_whole(
    lib: Library, monkeypatch: pytest.MonkeyPatch
) -> None:
    lib.add(3, version=1)
    lib.build()
    with lib.sessions.begin() as session:
        session.execute(sa.update(Layout).values(layout_version=2))

    def crash(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(atlases, "encode_manifest", crash)
    with pytest.raises(OSError, match="disk full"):
        lib.build()

    assert lib.sets() == ["1"]
    assert atlas_schedule.current_set(lib.derived_dir) == 1


def _assert_matches(sheet: Image.Image, cell: atlases.Cell, thumbnail: Path) -> None:
    left, top = cell.origin
    drawn = np.asarray(sheet.crop((left, top, left + cell.width, top + cell.height)), np.int16)
    with Image.open(thumbnail) as source:
        expected = np.asarray(source.convert("RGB"), np.int16)
    assert np.abs(drawn - expected).mean() < 6  # both are JPEGs


# When a set is due


def test_nothing_laid_out_and_no_set_is_not_due(lib: Library) -> None:
    assert not lib.queue_if_due()


def test_a_layout_with_no_set_is_due_once(lib: Library) -> None:
    lib.add(2)
    assert lib.queue_if_due()
    assert not lib.queue_if_due()  # one is queued already
    assert lib.atlases_jobs() == [JobStatus.PENDING]


def test_a_set_for_the_current_layout_is_not_due(lib: Library) -> None:
    lib.add(2)
    lib.build()
    assert not lib.queue_if_due()


def test_a_set_left_after_the_layout_is_gone_is_due(lib: Library) -> None:
    lib.add(2)
    lib.build()
    with lib.sessions.begin() as session:
        session.execute(sa.delete(Layout))
    assert lib.queue_if_due()


def test_a_parked_atlases_job_isnt_queued_again(lib: Library) -> None:
    lib.add(2)
    with lib.sessions.begin() as session:
        session.add(
            Job(
                stage=Stage.ATLASES,
                priority=PRIORITY[Stage.ATLASES],
                status=JobStatus.PARKED,
                attempts=5,
                enqueued_at=START,
            )
        )
    assert not lib.queue_if_due()


def test_the_worker_queues_the_atlases_after_a_layout_job(lib: Library, migrated: Engine) -> None:
    lib.add(2)
    runs: list[str] = []

    def run(stage: str) -> Runner:
        def runner(_: object) -> None:
            runs.append(stage)

        return runner

    worker = Worker(
        migrated,
        {Stage.LAYOUT: run(Stage.LAYOUT), Stage.ATLASES: run(Stage.ATLASES)},
        queue=lib.queue,
        clock=lambda: START,
        derived_dir=lib.derived_dir,
    )
    with lib.sessions.begin() as session:
        lib.queue.enqueue(session, Stage.LAYOUT)

    worker.drain()

    assert runs == [Stage.LAYOUT, Stage.ATLASES]


def test_a_reconcile_queues_the_atlases_for_a_layout_without_them(
    lib: Library, migrated: Engine
) -> None:
    lib.add(2)
    worker = Worker(migrated, {}, queue=lib.queue, clock=lambda: START, derived_dir=lib.derived_dir)

    assert worker.reconcile()[Stage.ATLASES] == 1


# The worker over tier 1


def test_worker_once_packs_the_tier_1_fixtures(
    migrated: Engine,
    settings: Settings,
    library: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "PHOTO_DIR": settings.photo_dir,
        "TRASH_DIR": settings.trash_dir,
        "DATA_DIR": settings.data_dir,
        "MODELS_DIR": settings.models_dir,
        "CLIP_MODEL": "fake",
    }.items():
        monkeypatch.setenv(name, str(value))
    # Its scan waits to retry, which holds the layout back (see test_layout.py).
    (library / "broken" / "truncated.jpg").unlink()

    assert main(["scan"]) == 0
    assert main(["--once"], clip=FakeClip()) == 0

    with sessionmaker(migrated)() as session:
        laid_out = set(session.scalars(sa.select(Layout.item_id)))
        version = session.scalar(sa.select(sa.func.max(Layout.layout_version)))
    assert laid_out and version == 1
    manifest = atlases.decode_manifest(
        (atlas_schedule.set_path(settings.derived_dir, 1) / atlas_schedule.MANIFEST).read_bytes()
    )
    assert {c.item_id for c in manifest.cells} == laid_out
    with Image.open(atlas_schedule.set_path(settings.derived_dir, 1) / "0.jpg") as sheet:
        for cell in manifest.cells:
            thumbnail = thumbnail_path(settings.derived_dir, cell.item_id, Thumbnail.MAP)
            with Image.open(thumbnail) as source:
                assert (cell.width, cell.height) == source.size
            _assert_matches(sheet, cell, thumbnail)
    # The 640 by 480 photos keep their shape.
    assert any((c.width, c.height) == (64, 48) for c in manifest.cells)


# The API


@pytest.fixture
def client(migrated: Engine, settings: Settings) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app(settings)), base_url="http://test")


@pytest.mark.anyio
async def test_before_any_set_the_manifest_is_empty(client: AsyncClient) -> None:
    response = await client.get("/api/map/atlases")
    assert response.status_code == 200
    manifest = atlases.decode_manifest(response.content)
    assert (manifest.version, manifest.sheets, manifest.cells) == (0, 0, [])


@pytest.mark.anyio
async def test_the_manifest_and_sheets_are_served_with_cache_headers(
    client: AsyncClient, lib: Library
) -> None:
    ids = lib.add(3)
    lib.build()

    response = await client.get("/api/map/atlases")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-cache"
    assert [c.item_id for c in atlases.decode_manifest(response.content).cells] == ids
    again = await client.get(
        "/api/map/atlases", headers={"If-None-Match": response.headers["etag"]}
    )
    assert again.status_code == 304

    sheet = await client.get("/api/map/atlases/1/0.jpg")
    assert sheet.status_code == 200
    assert sheet.headers["content-type"] == "image/jpeg"
    assert sheet.headers["cache-control"] == "private, no-cache"
    again = await client.get(
        "/api/map/atlases/1/0.jpg", headers={"If-None-Match": sheet.headers["etag"]}
    )
    assert again.status_code == 304


@pytest.mark.anyio
@pytest.mark.parametrize(
    "path",
    [
        "/api/map/atlases/2/0.jpg",
        "/api/map/atlases/1/1.jpg",
        "/api/map/atlases/0/0.jpg",
        "/api/map/atlases/1/-1.jpg",
    ],
)
async def test_a_sheet_that_doesnt_exist_is_a_404(
    client: AsyncClient, lib: Library, path: str
) -> None:
    lib.add(1)
    lib.build()
    assert (await client.get(path)).status_code == 404
