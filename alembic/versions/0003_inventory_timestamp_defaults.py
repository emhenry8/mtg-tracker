"""Add server-side defaults to inventory.created_at / inventory.updated_at.

Same issue as 0002, but for the inventory table: these columns predate
the current model and have no default, so inserting a new inventory
row (e.g. adding a card to the collection) fails with a NOT NULL
violation.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-22
"""

from alembic import op
import sqlalchemy as sa


revision = "0003"

down_revision = "0002"

branch_labels = None

depends_on = None


def upgrade():

    op.alter_column(
        "inventory",
        "created_at",
        server_default=sa.text("now()"),
    )

    op.alter_column(
        "inventory",
        "updated_at",
        server_default=sa.text("now()"),
    )


def downgrade():

    op.alter_column(
        "inventory",
        "created_at",
        server_default=None,
    )

    op.alter_column(
        "inventory",
        "updated_at",
        server_default=None,
    )
