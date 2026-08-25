"""Add Card.produced_mana (for the deck mana-base warning).

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-25
"""

from alembic import op
import sqlalchemy as sa


revision = "0006"

down_revision = "0005"

branch_labels = None

depends_on = None


def upgrade():

    bind = op.get_bind()

    inspector = sa.inspect(bind)


    columns = {
        column["name"]
        for column in inspector.get_columns("cards")
    }

    if "produced_mana" not in columns:

        op.add_column(
            "cards",
            sa.Column(
                "produced_mana",
                sa.String(100),
                nullable=True,
            ),
        )


def downgrade():

    op.drop_column("cards", "produced_mana")
