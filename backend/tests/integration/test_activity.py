"""`GET /api/activity` and the pause and resume endpoints (plan §7, §6.11)."""

import datetime as dt
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from photo_triage.api.app import create_app
from photo_triage.api.deps import current_actor
from photo_triage.db.models import Job, JobStatus, WorkerControl
from photo_triage.quiet_hours import parse
from photo_triage.settings import Settings
from photo_triage.worker import controls, heartbeat
from photo_triage.worker.loop import Worker
from photo_triage.worker.queue import PRIORITY, JobQueue, Stage

pytestmark = pytest.mark.anyio

START = dt.datetime(2026, 10, 5, 12, tzinfo=dt.UTC)  # a Monday

STAGES_IN_ORDER = [
    "metadata_write",
    "scan",
    "thumbnail",
    "clip",
    "quality",
    "faces",
    "recognize",
    "layout",
    "duplicates",
    "atlases",
    "llm_tag",
]


class FakeClock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def real_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def app(settings: Settings, clock: FakeClock, real_clock: FakeClock) -> Iterator[FastAPI]:
    app = create_app(settings, clock=clock, real_clock=real_clock)
    yield app
    app.state.engine.dispose()


@pytest.fixture
async def client(app: FastAPI, migrated: Engine) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _add_jobs(engine: Engine, stage: Stage, **counts: int) -> None:
    with Session(engine) as session:
        for status, count in counts.items():
            for _ in range(count):
                session.add(
                    Job(
                        stage=stage,
                        priority=PRIORITY[stage],
                        status=JobStatus(status),
                        attempts=0,
                        enqueued_at=START,
                    )
                )
        session.commit()


def _beat(engine: Engine, at: dt.datetime) -> None:
    with Session(engine) as session, session.begin():
        heartbeat.beat(session, started_at=at, now=at)


def _stages(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {row["stage"]: row for row in body["stages"]}


async def test_every_stage_is_listed_in_priority_order_with_its_counts(
    client: AsyncClient, migrated: Engine
) -> None:
    _add_jobs(migrated, Stage.CLIP, done=3, pending=2, running=1, error=4, parked=5)
    _add_jobs(migrated, Stage.LAYOUT, done=1)

    response = await client.get("/api/activity")

    assert response.status_code == 200
    body = response.json()
    assert [row["stage"] for row in body["stages"]] == STAGES_IN_ORDER
    stages = _stages(body)
    assert stages["clip"] == {
        "stage": "clip",
        "kind": "item",
        "paused": False,
        "done": 3,
        "total": 15,
        "pending": 2,
        "running": 1,
        "errored": 4,
        "parked": 5,
    }
    assert stages["layout"]["kind"] == "batch"
    assert (stages["layout"]["done"], stages["layout"]["total"]) == (1, 1)
    # A stage with no jobs still has its row.
    assert stages["scan"] == {
        "stage": "scan",
        "kind": "item",
        "paused": False,
        "done": 0,
        "total": 0,
        "pending": 0,
        "running": 0,
        "errored": 0,
        "parked": 0,
    }


async def test_the_noop_stage_is_listed_only_when_enabled(
    settings: Settings, migrated: Engine
) -> None:
    app = create_app(settings.model_copy(update={"worker_noop_stage": True}))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = (await client.get("/api/activity")).json()
    app.state.engine.dispose()

    assert [row["stage"] for row in body["stages"]] == [*STAGES_IN_ORDER, "noop"]


async def test_a_worker_that_has_never_run_is_stopped(client: AsyncClient) -> None:
    worker = (await client.get("/api/activity")).json()["worker"]

    assert worker == {
        "status": "stopped",
        "paused": False,
        "quiet_until_at": None,
        "last_seen_at": None,
    }


async def test_the_worker_is_stopped_once_its_heartbeat_is_stale(
    client: AsyncClient, migrated: Engine, real_clock: FakeClock
) -> None:
    _beat(migrated, START)

    worker = (await client.get("/api/activity")).json()["worker"]
    assert (worker["status"], worker["last_seen_at"]) == ("running", "2026-10-05T12:00:00Z")

    real_clock.now = START + heartbeat.STALE_AFTER - dt.timedelta(seconds=1)
    assert (await client.get("/api/activity")).json()["worker"]["status"] == "running"

    real_clock.now = START + heartbeat.STALE_AFTER
    worker = (await client.get("/api/activity")).json()["worker"]
    assert (worker["status"], worker["last_seen_at"]) == ("stopped", "2026-10-05T12:00:00Z")


async def test_a_clean_stop_shows_at_once(client: AsyncClient, migrated: Engine) -> None:
    _beat(migrated, START)
    with Session(migrated) as session, session.begin():
        heartbeat.stopped(session, START)

    assert (await client.get("/api/activity")).json()["worker"]["status"] == "stopped"


async def test_quiet_hours_show_when_they_end(
    settings: Settings, migrated: Engine, clock: FakeClock, real_clock: FakeClock
) -> None:
    settings = settings.model_copy(update={"worker_quiet_hours": parse("Mon 11:00-14:00")})
    app = create_app(settings, clock=clock, real_clock=real_clock)
    _beat(migrated, START)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        worker = (await client.get("/api/activity")).json()["worker"]
    app.state.engine.dispose()

    assert (worker["status"], worker["quiet_until_at"]) == ("quiet", "2026-10-05T14:00:00Z")


async def test_pausing_and_resuming_the_worker(
    app: FastAPI, client: AsyncClient, migrated: Engine
) -> None:
    _beat(migrated, START)
    app.dependency_overrides[current_actor] = lambda: "alice@example.com"

    response = await client.post("/api/worker/pause")

    assert response.status_code == 200
    assert response.json()["worker"]["status"] == "paused"
    assert response.json()["worker"]["paused"] is True
    assert (await client.get("/api/activity")).json()["worker"]["status"] == "paused"
    with Session(migrated) as session:
        control = session.get_one(WorkerControl, controls.GLOBAL)
        assert (control.paused, control.actor, control.changed_at) == (
            True,
            "alice@example.com",
            START,
        )
    # The worker sees it too.
    _add_jobs(migrated, Stage.LAYOUT, pending=1)
    worker = Worker(migrated, {Stage.LAYOUT: lambda job: None}, queue=JobQueue(lambda: START))
    assert worker.drain() == 0

    response = await client.post("/api/worker/resume")

    assert response.json()["worker"]["status"] == "running"
    assert worker.drain() == 1


async def test_pausing_and_resuming_a_stage(
    app: FastAPI, client: AsyncClient, migrated: Engine
) -> None:
    _beat(migrated, START)

    response = await client.post("/api/worker/stages/layout/pause")

    stages = _stages(response.json())
    assert stages["layout"]["paused"] is True
    assert stages["clip"]["paused"] is False
    assert response.json()["worker"]["status"] == "running"
    with Session(migrated) as session:
        assert session.get_one(WorkerControl, "layout").actor == "anonymous"
    _add_jobs(migrated, Stage.LAYOUT, pending=1)
    _add_jobs(migrated, Stage.ATLASES, pending=1)
    ran: list[str] = []
    worker = Worker(
        migrated,
        dict.fromkeys([Stage.LAYOUT, Stage.ATLASES], lambda job: ran.append(job.stage)),
        queue=JobQueue(lambda: START),
    )
    assert worker.drain() == 1
    assert ran == ["atlases"]

    response = await client.post("/api/worker/stages/layout/resume")

    assert _stages(response.json())["layout"]["paused"] is False
    assert worker.drain() == 1
    assert ran == ["atlases", "layout"]


async def test_a_global_pause_is_shown_while_the_worker_is_stopped(
    client: AsyncClient,
) -> None:
    worker = (await client.post("/api/worker/pause")).json()["worker"]

    assert (worker["status"], worker["paused"]) == ("stopped", True)


async def test_an_unknown_stage_is_rejected(client: AsyncClient) -> None:
    response = await client.post("/api/worker/stages/bogus/pause")

    assert response.status_code == 422


async def test_the_openapi_schema_describes_the_responses(client: AsyncClient) -> None:
    schema = (await client.get("/api/openapi.json")).json()

    def response_schema(path: str, method: str, media_type: str) -> Any:
        return schema["paths"][path][method]["responses"]["200"]["content"][media_type]

    activity = {"$ref": "#/components/schemas/Activity"}
    assert response_schema("/api/activity", "get", "application/json")["schema"] == activity
    for path in [
        "/api/worker/pause",
        "/api/worker/resume",
        "/api/worker/stages/{stage}/pause",
        "/api/worker/stages/{stage}/resume",
    ]:
        assert response_schema(path, "post", "application/json")["schema"] == activity
    events = response_schema("/api/events", "get", "text/event-stream")
    assert events["itemSchema"]["properties"]["data"]["contentSchema"] == activity
    components = schema["components"]["schemas"]
    assert set(components["Activity"]["properties"]) == {"worker", "stages"}
    assert set(components["WorkerActivity"]["properties"]) == {
        "status",
        "paused",
        "quiet_until_at",
        "last_seen_at",
    }
    assert components["StageActivity"]["properties"]["total"]["type"] == "integer"
