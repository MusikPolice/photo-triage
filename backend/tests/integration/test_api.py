"""The API skeleton: health check, OpenAPI docs, current_actor and the frontend
(plan §4, §5, §10)."""

import logging
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from httpx import ASGITransport, AsyncClient

from photo_triage.api.app import create_app
from photo_triage.api.deps import Actor, current_actor
from photo_triage.settings import Settings

pytestmark = pytest.mark.anyio


async def test_health_returns_ok(client: AsyncClient) -> None:
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_openapi_docs_load(client: AsyncClient) -> None:
    docs = await client.get("/api/docs")
    assert docs.status_code == 200
    schema = await client.get("/api/openapi.json")
    assert "/api/health" in schema.json()["paths"]


async def test_current_actor_is_anonymous_without_auth(app: FastAPI, client: AsyncClient) -> None:
    @app.get("/api/test-actor")
    def whoami(actor: Actor) -> str:  # pyright: ignore[reportUnusedFunction]
        return actor

    response = await client.get("/api/test-actor")
    assert response.json() == "anonymous"


async def test_routes_get_identity_only_from_current_actor(
    app: FastAPI, client: AsyncClient
) -> None:
    # Overriding the one dependency changes the actor every route sees.
    @app.get("/api/test-actor")
    def whoami(actor: Annotated[str, Depends(current_actor)]) -> str:  # pyright: ignore[reportUnusedFunction]
        return actor

    app.dependency_overrides[current_actor] = lambda: "alice@example.com"
    response = await client.get("/api/test-actor")
    assert response.json() == "alice@example.com"


async def test_unhandled_error_is_logged_with_traceback_and_returns_500(
    app: FastAPI, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    @app.get("/api/test-boom")
    def boom() -> None:  # pyright: ignore[reportUnusedFunction]
        raise RuntimeError("kaboom")

    response = await client.get("/api/test-boom")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal Server Error"}
    [record] = [r for r in caplog.records if r.name == "photo_triage.api.errors"]
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Unhandled error in GET /api/test-boom"
    assert "RuntimeError: kaboom" in caplog.text
    assert "Traceback (most recent call last)" in caplog.text


async def test_http_errors_are_not_logged_as_unhandled(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    response = await client.get("/api/no-such-route")

    assert response.status_code == 404
    assert not [r for r in caplog.records if r.name == "photo_triage.api.errors"]


async def test_error_after_a_response_starts_goes_to_the_server(
    app: FastAPI, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    # A streamed response can't be swapped for a 500 once it has started, so the
    # server gets the exception (and logs it) instead.
    def chunks() -> Iterator[str]:
        yield "first"
        raise RuntimeError("mid-stream")

    @app.get("/api/test-stream")
    def stream() -> StreamingResponse:  # pyright: ignore[reportUnusedFunction]
        return StreamingResponse(chunks())

    with pytest.raises(RuntimeError, match="mid-stream"):
        await client.get("/api/test-stream")
    assert not [r for r in caplog.records if r.name == "photo_triage.api.errors"]


@pytest.fixture
async def with_frontend(settings: Settings, tmp_path: Path) -> AsyncIterator[AsyncClient]:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>photo-triage</title>")
    (dist / "assets" / "index.js").write_text("console.log('hi')")
    app = create_app(settings, frontend_dir=dist)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.state.engine.dispose()


async def test_the_frontend_is_served_at_the_root(with_frontend: AsyncClient) -> None:
    index = await with_frontend.get("/")
    assert index.status_code == 200
    assert index.headers["content-type"].startswith("text/html")
    assert "<title>photo-triage</title>" in index.text

    asset = await with_frontend.get("/assets/index.js")
    assert asset.status_code == 200
    assert "javascript" in asset.headers["content-type"]


@pytest.mark.parametrize("path", ["/activity", "/activity/", "/map/some/where"])
async def test_the_frontends_pages_get_index_html(with_frontend: AsyncClient, path: str) -> None:
    page = await with_frontend.get(path)
    assert page.status_code == 200
    assert "<title>photo-triage</title>" in page.text


@pytest.mark.parametrize("path", ["/assets/missing.js", "/favicon.ico", "/api", "/api/x/y"])
async def test_missing_files_and_api_routes_are_404s(with_frontend: AsyncClient, path: str) -> None:
    missing = await with_frontend.get(path)
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}


async def test_the_api_comes_before_the_frontend(with_frontend: AsyncClient) -> None:
    assert (await with_frontend.get("/api/health")).json() == {"status": "ok"}
    assert (await with_frontend.get("/api/docs")).status_code == 200

    missing = await with_frontend.get("/api/no-such-route")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}


async def test_no_frontend_unless_given_one(client: AsyncClient) -> None:
    assert (await client.get("/")).status_code == 404
