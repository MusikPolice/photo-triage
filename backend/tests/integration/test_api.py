"""The API skeleton: health check, OpenAPI docs and current_actor (plan §4, §10)."""

import logging
from collections.abc import Iterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from fastapi.responses import StreamingResponse
from httpx import AsyncClient

from photo_triage.api.deps import Actor, current_actor

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
