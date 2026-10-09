"""Migrating the database, and the schema check the API and worker run at startup.

Migrating is one explicit step, run before anything else starts
(`python -m photo_triage.db migrate`, or `just db-migrate`). The API and the worker
are separate processes sharing the database, so neither migrates: each checks the
database is at the head revision and exits if it isn't.
"""

import datetime as dt
import logging
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext, MigrationInfo
from alembic.script import ScriptDirectory
from sqlalchemy.engine import Engine

from photo_triage.db.engine import DB_FILENAME, create_engine, database_url, open_database
from photo_triage.files import prune_backups
from photo_triage.settings import Settings

logger = logging.getLogger(__name__)

MIGRATIONS = "photo_triage.db:migrations"
MIGRATE_COMMAND = "`python -m photo_triage.db migrate` (`just db-migrate` in dev)"

BACKUPS_KEPT = 5
"""The migrate step keeps this many backups in `DATA_DIR/backups`, newest first."""


class SchemaError(Exception):
    """The database isn't at the revision this code expects."""


class SchemaBehindError(SchemaError):
    """The database is missing, empty or at an older revision: migrate it."""


class SchemaAheadError(SchemaError):
    """The database was migrated by newer code than this."""


def backups_dir(settings: Settings) -> Path:
    return settings.data_dir / "backups"


def alembic_config(url: str) -> Config:
    """Alembic run from the packaged migrations, without `alembic.ini`. Logging stays
    as the caller configured it (see `migrations/env.py`)."""
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    config.attributes["configure_logger"] = False
    return config


def head_revision() -> str:
    head = ScriptDirectory.from_config(alembic_config("")).get_current_head()
    if head is None:
        raise RuntimeError("there are no migrations")
    return head


def current_revision(engine: Engine) -> str | None:
    """The database's revision, or None if it has never been migrated."""
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def check_schema(data_dir: Path) -> None:
    """Raise a `SchemaError` saying what to do unless the database in `data_dir` is at
    the head revision. Doesn't create the database if it's missing."""
    path = data_dir / DB_FILENAME
    head = head_revision()
    if not path.is_file():
        raise SchemaBehindError(
            f"There's no database at {path}; this code expects revision {head}. "
            f"Run {MIGRATE_COMMAND} first."
        )
    engine = create_engine(database_url(data_dir))
    try:
        current = current_revision(engine)
    finally:
        engine.dispose()
    _check_revision(current, head)


def _check_revision(current: str | None, head: str) -> None:
    if current == head:
        return
    if current is not None and not _known(current):
        raise _ahead(current, head)
    raise SchemaBehindError(
        f"The database is at revision {current or 'none (not migrated)'}, but this code "
        f"expects {head}. Run {MIGRATE_COMMAND} first."
    )


def _ahead(current: str, head: str) -> SchemaAheadError:
    return SchemaAheadError(
        f"The database is newer than this code: it's at revision {current}, which "
        f"this code doesn't have (its newest is {head}). Run the newer code, or "
        "restore a backup from DATA_DIR/backups."
    )


def _known(revision: str) -> bool:
    script = ScriptDirectory.from_config(alembic_config(""))
    return any(r.revision == revision for r in script.walk_revisions())


def migrate(settings: Settings, *, now: dt.datetime | None = None) -> Path | None:
    """Upgrade the database in `DATA_DIR` to head, creating it if needed.

    An existing database that needs upgrading is first copied to `DATA_DIR/backups`,
    and only the newest `BACKUPS_KEPT` backups are kept. Returns the backup's path,
    or None if none was needed. Raises `SchemaAheadError`, changing nothing, if the
    database is newer than this code.
    """
    head = head_revision()
    engine = open_database(settings.data_dir)
    try:
        current = current_revision(engine)
        if current == head:
            logger.info("The database is already at revision %s; nothing to do", head)
            return None
        if current is not None and not _known(current):
            raise _ahead(current, head)

        backup = None
        if _has_tables(engine):
            backup = _back_up(engine, settings, current, now or dt.datetime.now(dt.UTC))
        else:
            logger.info("Creating the database at %s", settings.data_dir / DB_FILENAME)

        config = alembic_config(database_url(settings.data_dir))
        docs = {s.revision: s.doc for s in ScriptDirectory.from_config(config).walk_revisions()}

        def log_applied(*, step: MigrationInfo, **_kwargs: Any) -> None:
            for revision in step.up_revision_ids:
                logger.info("Applied migration %s: %s", revision, docs[revision])

        config.attributes["on_version_apply"] = log_applied
        command.upgrade(config, "head")
        logger.info("The database is at revision %s", head)
        return backup
    finally:
        engine.dispose()


def _has_tables(engine: Engine) -> bool:
    return bool(sa.inspect(engine).get_table_names())


def _back_up(engine: Engine, settings: Settings, revision: str | None, now: dt.datetime) -> Path:
    directory = backups_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"{stamp}-from-{revision or 'none'}.db"
    # A consistent copy, WAL included, written by SQLite. It fails if `path` exists.
    with engine.connect() as connection:
        connection.exec_driver_sql("VACUUM INTO ?", (str(path),))
    logger.info("Backed up the database to %s", path)
    for old in prune_backups(directory, keep=BACKUPS_KEPT):
        logger.info("Deleted the old backup %s", old)
    return path
