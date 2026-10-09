"""The SQLite database in `DATA_DIR` (plan §5: SQLite via SQLAlchemy, WAL mode)."""

from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import ConnectionPoolEntry

DB_FILENAME = "photo-triage.db"

BUSY_TIMEOUT_MS = 5000
"""How long a connection waits on another process's write lock before failing.
The API and the worker are separate processes sharing one database."""


def database_url(data_dir: Path) -> str:
    return f"sqlite:///{data_dir / DB_FILENAME}"


def create_engine(url: str) -> Engine:
    """An engine whose every connection uses WAL and enforces foreign keys."""
    engine = sa.create_engine(url)
    event.listen(engine, "connect", _configure_connection)
    return engine


def open_database(data_dir: Path) -> Engine:
    """The engine for the database in `data_dir`, creating the directory if needed.

    Opening doesn't migrate: `python -m photo_triage.db migrate` (`just db-migrate`) does.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    return create_engine(database_url(data_dir))


def _configure_connection(dbapi_connection: Any, _record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()
