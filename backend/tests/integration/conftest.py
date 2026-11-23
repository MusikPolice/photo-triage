from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import Engine
from vectors import Library

from photo_triage.api.app import create_app
from photo_triage.api.similarity import Similarity
from photo_triage.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# Items with CLIP vectors, for neighbours and search (`vectors.py`)
# ---------------------------------------------------------------------------


@pytest.fixture
def lib(migrated: Engine, settings: Settings) -> Library:
    return Library(migrated, settings.clip_model)


@pytest.fixture
def fresh(app: FastAPI, migrated: Engine, settings: Settings) -> None:
    """Read the matrix again on every request in which the worker has embedded."""
    app.state.similarity = Similarity(migrated, settings.clip_model, reload_min_interval_s=0)
