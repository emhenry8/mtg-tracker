"""Add Card.back_card_id (manual link for double-sided token pairs).

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-04
"""

from alembic import op
import sqlalchemy as sa


revision = "0007"

down_revision = "0006"

branch_labels = None

depends_on = None


def upgrade():

    bind = op.get_bind()

    inspector = sa.inspect(bind)


    columns = {
        column["name"]
        for column in inspector.get_columns("cards")
    }

    if "back_card_id" not in columns:

        op.add_column(
            "cards",
            sa.Column(
                "back_card_id",
                sa.Integer(),
                nullable=True,
            ),
        )

        op.create_foreign_key(
            "fk_cards_back_card_id",
            "cards",
            "cards",
            ["back_card_id"],
            ["id"],
            ondelete="SET NULL",
        )

        op.create_index(
            "ix_cards_back_card_id",
            "cards",
            ["back_card_id"],
        )


def downgrade():

    op.drop_index("ix_cards_back_card_id", table_name="cards")

    op.drop_constraint(
        "fk_cards_back_card_id",
        "cards",
        type_="foreignkey",
    )

    op.drop_column("cards", "back_card_id")
