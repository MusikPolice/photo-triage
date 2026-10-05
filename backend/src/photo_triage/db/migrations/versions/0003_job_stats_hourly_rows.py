"""job_stats: hourly rows keyed by hour_start_at

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-04 19:10:06.411725
"""

# Written by hand: basic, not strict, type checking.
# pyright: basic

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# SQLAlchemy stores a DateTime in SQLite as 'YYYY-MM-DD HH:MM:SS.ffffff' and a Date
# as 'YYYY-MM-DD'. A daily row becomes the row for its first hour (UTC), and
# downgrading adds each day's hours back up.
COUNTS = "processed, errors, busy_seconds"


def _create(name: str, key: sa.Column) -> None:
    op.create_table(
        name,
        key,
        sa.Column("stage", sa.String(length=32), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("errors", sa.Integer(), nullable=False),
        sa.Column("busy_seconds", sa.Double(), nullable=False),
        sa.PrimaryKeyConstraint(key.name, "stage", name="pk_job_stats"),
    )


def upgrade() -> None:
    op.rename_table("job_stats", "job_stats_daily")
    _create("job_stats", sa.Column("hour_start_at", sa.DateTime(), nullable=False))
    op.execute(
        f"INSERT INTO job_stats (hour_start_at, stage, {COUNTS}) "
        f"SELECT date || ' 00:00:00.000000', stage, {COUNTS} FROM job_stats_daily"
    )
    op.drop_table("job_stats_daily")


def downgrade() -> None:
    op.rename_table("job_stats", "job_stats_hourly")
    _create("job_stats", sa.Column("date", sa.Date(), nullable=False))
    op.execute(
        f"INSERT INTO job_stats (date, stage, {COUNTS}) "
        "SELECT substr(hour_start_at, 1, 10), stage, "
        "sum(processed), sum(errors), sum(busy_seconds) "
        "FROM job_stats_hourly GROUP BY substr(hour_start_at, 1, 10), stage"
    )
    op.drop_table("job_stats_hourly")
