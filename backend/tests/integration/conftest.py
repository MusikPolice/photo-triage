from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import Engine

from photo_triage.api.app import create_app
from photo_triage.db.engine import open_database
from photo_triage.settings import Settings

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


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


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = open_database(tmp_path / "data")
    yield engine
    engine.dispose()


@pytest.fixture
def alembic_cfg(engine: Engine) -> Config:
    cfg = Config(ALEMBIC_INI)
    cfg.set_main_option("sqlalchemy.url", engine.url.render_as_string(hide_password=False))
    return cfg


@pytest.fixture
def migrated(engine: Engine, alembic_cfg: Config) -> Engine:
    """A database upgraded to the newest migration."""
    command.upgrade(alembic_cfg, "head")
    return engine
