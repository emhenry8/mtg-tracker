"""Add server-side defaults to cards.created_at / cards.updated_at.

These columns predate the current model and were never given a
default, so inserts that don't set them explicitly (e.g. adding a new
card) fail with a NOT NULL violation.

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-22
"""

from alembic import op
import sqlalchemy as sa


revision = "0002"

down_revision = "0001"

branch_labels = None

depends_on = None


def upgrade():

    op.alter_column(
        "cards",
        "created_at",
        server_default=sa.text("now()"),
    )

    op.alter_column(
        "cards",
        "updated_at",
        server_default=sa.text("now()"),
    )


def downgrade():

    op.alter_column(
        "cards",
        "created_at",
        server_default=None,
    )

    op.alter_column(
        "cards",
        "updated_at",
        server_default=None,
    )
