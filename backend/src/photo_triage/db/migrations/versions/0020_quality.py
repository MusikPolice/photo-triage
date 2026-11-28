"""quality signals per item

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-27 18:00:00.000000
"""

# Written by hand and checked against the models: basic, not strict, type checking.
# pyright: basic

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "quality",
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("blur", sa.Float(), nullable=False),
        sa.Column("exposure", sa.Float(), nullable=False),
        sa.Column("dark_clipped_fraction", sa.Float(), nullable=False),
        sa.Column("bright_clipped_fraction", sa.Float(), nullable=False),
        sa.Column("mean_brightness", sa.Float(), nullable=False),
        sa.Column("resolution", sa.Float(), nullable=False),
        sa.Column("aesthetic", sa.Float(), nullable=True),
        sa.Column("junk_label", sa.String(length=32), nullable=True),
        sa.Column("junk_score", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_quality_item_id_items"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("item_id", name=op.f("pk_quality")),
    )


def downgrade() -> None:
    op.drop_table("quality")
