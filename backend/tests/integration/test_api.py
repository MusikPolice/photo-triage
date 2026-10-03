"""The API skeleton: health check, OpenAPI docs and current_actor (plan §4, §10)."""

from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
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
