"""Add decks and deck_cards tables for the deck builder.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-23
"""

from alembic import op
import sqlalchemy as sa


revision = "0004"

down_revision = "0003"

branch_labels = None

depends_on = None


def upgrade():

    bind = op.get_bind()

    inspector = sa.inspect(bind)


    if not inspector.has_table("decks"):

        op.create_table(

            "decks",

            sa.Column(
                "id",
                sa.Integer(),
                primary_key=True,
            ),

            sa.Column(
                "name",
                sa.String(255),
                nullable=False,
            ),

            sa.Column(
                "notes",
                sa.Text(),
                nullable=True,
            ),

            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),

            sa.Column(
                "updated_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )


    if not inspector.has_table("deck_cards"):

        op.create_table(

            "deck_cards",

            sa.Column(
                "id",
                sa.Integer(),
                primary_key=True,
            ),

            sa.Column(
                "deck_id",
                sa.Integer(),
                sa.ForeignKey(
                    "decks.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),

            sa.Column(
                "card_id",
                sa.Integer(),
                sa.ForeignKey(
                    "cards.id",
                    ondelete="CASCADE",
                ),
                nullable=False,
            ),

            sa.Column(
                "section",
                sa.String(20),
                nullable=False,
                server_default="mainboard",
            ),

            sa.Column(
                "quantity",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),

            sa.UniqueConstraint(
                "deck_id",
                "card_id",
                "section",
                name="uq_deck_card_section",
            ),
        )


def downgrade():

    op.drop_table("deck_cards")

    op.drop_table("decks")
