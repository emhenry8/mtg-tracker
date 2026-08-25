"""Add Card.legalities (for the Standard legality warning) and a
collection_value_snapshots table (for the value-over-time chart).

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-24
"""

from alembic import op
import sqlalchemy as sa


revision = "0005"

down_revision = "0004"

branch_labels = None

depends_on = None


def upgrade():

    bind = op.get_bind()

    inspector = sa.inspect(bind)


    columns = {
        column["name"]
        for column in inspector.get_columns("cards")
    }

    if "legalities" not in columns:

        op.add_column(
            "cards",
            sa.Column(
                "legalities",
                sa.Text(),
                nullable=True,
            ),
        )


    if not inspector.has_table("collection_value_snapshots"):

        op.create_table(

            "collection_value_snapshots",

            sa.Column(
                "id",
                sa.Integer(),
                primary_key=True,
            ),

            sa.Column(
                "captured_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),

            sa.Column(
                "total_cards",
                sa.Integer(),
                nullable=False,
            ),

            sa.Column(
                "total_value",
                sa.Numeric(12, 2),
                nullable=False,
            ),
        )


def downgrade():

    op.drop_table("collection_value_snapshots")

    op.drop_column("cards", "legalities")
