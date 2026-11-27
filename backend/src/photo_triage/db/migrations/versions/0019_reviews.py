"""reviews, duplicate group leases, and items' reviewed markers

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-24 13:00:00.000000
"""

# Written by hand and checked against the models: basic, not strict, type checking.
# pyright: basic

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reviews",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column(
            "kind",
            sa.Enum(
                "quality",
                "duplicate",
                name="review_kind",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column(
            "decision",
            sa.Enum(
                "keep",
                "delete",
                "skip",
                name="decision",
                native_enum=False,
                create_constraint=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=255), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("kind IN ('quality', 'duplicate')", name=op.f("ck_reviews_review_kind")),
        sa.CheckConstraint(
            "decision IN ('keep', 'delete', 'skip')", name=op.f("ck_reviews_decision")
        ),
        sa.ForeignKeyConstraint(
            ["item_id"], ["items.id"], name=op.f("fk_reviews_item_id_items"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reviews")),
    )
    op.create_index("ix_reviews_item_id", "reviews", ["item_id"], unique=False)
    with op.batch_alter_table("duplicate_groups") as batch:
        batch.add_column(sa.Column("leased_to", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    with op.batch_alter_table("items") as batch:
        batch.add_column(
            sa.Column("reviewed", sa.JSON(), server_default=sa.text("'[]'"), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("items") as batch:
        batch.drop_column("reviewed")
    with op.batch_alter_table("duplicate_groups") as batch:
        batch.drop_column("lease_expires_at")
        batch.drop_column("leased_to")
    op.drop_index("ix_reviews_item_id", table_name="reviews")
    op.drop_table("reviews")
