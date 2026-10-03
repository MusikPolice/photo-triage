"""Alembic environment. Migrations run online, through the app's engine."""

import sys
from logging.config import fileConfig
from typing import Any, Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy.engine import Engine

from photo_triage.db.columns import UTCDateTime
from photo_triage.db.engine import create_engine, open_database
from photo_triage.db.models import Base
from photo_triage.settings import SettingsError, load_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)


def _engine() -> Engine:
    """`sqlalchemy.url` if set (tests set it), otherwise the database in DATA_DIR."""
    url = config.get_main_option("sqlalchemy.url")
    if url:
        return create_engine(url)
    try:
        return open_database(load_settings().data_dir)
    except SettingsError as e:
        sys.exit(str(e))


def _render_item(kind: str, obj: Any, _context: AutogenContext) -> str | Literal[False]:
    """Write app-defined column types as their plain SQLAlchemy type, so migrations
    don't import app code that may later change."""
    if kind == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def run_migrations() -> None:
    engine = _engine()
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=Base.metadata,
            # SQLite can't alter most columns in place; batch mode copies the table.
            render_as_batch=True,
            render_item=_render_item,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) migrations aren't supported.")
run_migrations()
