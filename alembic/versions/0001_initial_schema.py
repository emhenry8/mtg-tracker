"""Bring existing MTG database to current schema.

Revision ID: 0001
Revises:
Create Date: 2026-08-22
"""

from alembic import op
import sqlalchemy as sa


revision = "0001"

down_revision = None

branch_labels = None

depends_on = None


def column_exists(
    inspector,
    table_name,
    column_name,
):
    columns = inspector.get_columns(
        table_name
    )

    return any(
        column["name"] == column_name
        for column in columns
    )


def add_column_if_missing(
    inspector,
    table_name,
    column,
):
    if not column_exists(
        inspector,
        table_name,
        column.name,
    ):
        op.add_column(
            table_name,
            column,
        )


def upgrade():

    bind = op.get_bind()

    inspector = sa.inspect(bind)


    # ==================================================
    # CARDS
    # ==================================================

    if not inspector.has_table("cards"):

        op.create_table(

            "cards",

            sa.Column(
                "id",
                sa.Integer(),
                primary_key=True,
            ),

            sa.Column(
                "scryfall_id",
                sa.String(100),
                nullable=True,
            ),

            sa.Column(
                "name",
                sa.String(255),
                nullable=False,
            ),

            sa.Column(
                "set_code",
                sa.String(10),
                nullable=False,
            ),

            sa.Column(
                "set_name",
                sa.String(255),
                nullable=True,
            ),

            sa.Column(
                "collector_number",
                sa.String(50),
                nullable=False,
            ),

            sa.Column(
                "type_line",
                sa.String(500),
                nullable=True,
            ),

            sa.Column(
                "rarity",
                sa.String(50),
                nullable=True,
            ),

            sa.Column(
                "mana_cost",
                sa.String(255),
                nullable=True,
            ),

            sa.Column(
                "oracle_text",
                sa.Text(),
                nullable=True,
            ),

            sa.Column(
                "colors",
                sa.String(100),
                nullable=True,
            ),

            sa.Column(
                "image_url",
                sa.String(1000),
                nullable=True,
            ),

            sa.Column(
                "cmc",
                sa.Float(),
                nullable=True,
            ),

            sa.Column(
                "price_usd",
                sa.Numeric(12, 2),
                nullable=True,
            ),

            sa.Column(
                "price_usd_foil",
                sa.Numeric(12, 2),
                nullable=True,
            ),

            sa.Column(
                "price_usd_etched",
                sa.Numeric(12, 2),
                nullable=True,
            ),

            sa.Column(
                "price_updated_at",
                sa.DateTime(),
                nullable=True,
            ),

            sa.UniqueConstraint(
                "set_code",
                "collector_number",
                name="uq_card_set_collector",
            ),
        )

    else:

        inspector = sa.inspect(bind)

        # ----------------------------------------------
        # Existing cards table:
        # add missing columns
        # ----------------------------------------------

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "scryfall_id",
                sa.String(100),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "set_name",
                sa.String(255),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "type_line",
                sa.String(500),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "rarity",
                sa.String(50),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "mana_cost",
                sa.String(255),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "oracle_text",
                sa.Text(),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "colors",
                sa.String(100),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "image_url",
                sa.String(1000),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "cmc",
                sa.Float(),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "price_usd",
                sa.Numeric(12, 2),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "price_usd_foil",
                sa.Numeric(12, 2),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "price_usd_etched",
                sa.Numeric(12, 2),
                nullable=True,
            ),
        )

        add_column_if_missing(
            inspector,
            "cards",
            sa.Column(
                "price_updated_at",
                sa.DateTime(),
                nullable=True,
            ),
        )


    # ==================================================
    # INVENTORY
    # ==================================================

    inspector = sa.inspect(bind)


    if not inspector.has_table("inventory"):

        op.create_table(

            "inventory",

            sa.Column(
                "id",
                sa.Integer(),
                primary_key=True,
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
                "finish",
                sa.String(100),
                nullable=False,
                server_default="normal",
            ),

            sa.Column(
                "treatment",
                sa.String(255),
                nullable=False,
                server_default="regular",
            ),

            sa.Column(
                "quantity",
                sa.Integer(),
                nullable=False,
                server_default="1",
            ),

            sa.UniqueConstraint(
                "card_id",
                "finish",
                "treatment",
                name="uq_inventory_variant",
            ),
        )


def downgrade():

    # We intentionally do not delete the existing
    # collection database in the initial migration.

    pass
