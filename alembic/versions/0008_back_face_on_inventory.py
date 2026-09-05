"""Move back-face linking from Card to Inventory.

A single card number can legitimately pair with different backs
across different physical print runs (WotC reuses generic filler
tokens as the back of many unrelated front designs), so pairing has
to live on the specific batch of physical copies (Inventory), not on
the card's catalog identity (Card) — otherwise two distinct pairings
for the same front collapse into one.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-05
"""

from alembic import op
import sqlalchemy as sa


revision = "0008"

down_revision = "0007"

branch_labels = None

depends_on = None


def upgrade():

    bind = op.get_bind()

    inspector = sa.inspect(bind)


    inventory_columns = {
        column["name"]
        for column in inspector.get_columns("inventory")
    }

    if "back_card_id" not in inventory_columns:

        op.add_column(
            "inventory",
            sa.Column(
                "back_card_id",
                sa.Integer(),
                nullable=True,
            ),
        )

        op.create_foreign_key(
            "fk_inventory_back_card_id",
            "inventory",
            "cards",
            ["back_card_id"],
            ["id"],
            ondelete="SET NULL",
        )


    # Carry forward any existing Card-level links: whichever side
    # currently holds the inventory (always the primary/lower-number
    # side, per the old merge-on-link behavior) gets that quantity's
    # rows tagged with the old partner as their back_card_id.
    bind.execute(
        sa.text(
            """
            UPDATE inventory
            SET back_card_id = cards.back_card_id
            FROM cards
            WHERE inventory.card_id = cards.id
              AND cards.back_card_id IS NOT NULL
              AND inventory.back_card_id IS NULL
            """
        )
    )


    existing_indexes = {
        index["name"]
        for index in inspector.get_indexes("inventory")
    }

    existing_constraints = {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "inventory"
        )
    }

    if "uq_inventory_card_finish_treatment" in existing_constraints:

        op.drop_constraint(
            "uq_inventory_card_finish_treatment",
            "inventory",
            type_="unique",
        )

    if "uq_inventory_unlinked" not in existing_indexes:

        op.create_index(
            "uq_inventory_unlinked",
            "inventory",
            ["card_id", "finish", "treatment"],
            unique=True,
            postgresql_where=sa.text("back_card_id IS NULL"),
        )

    if "uq_inventory_linked" not in existing_indexes:

        op.create_index(
            "uq_inventory_linked",
            "inventory",
            ["card_id", "finish", "treatment", "back_card_id"],
            unique=True,
            postgresql_where=sa.text("back_card_id IS NOT NULL"),
        )


    card_columns = {
        column["name"]
        for column in inspector.get_columns("cards")
    }

    if "back_card_id" in card_columns:

        op.drop_index("ix_cards_back_card_id", table_name="cards")

        op.drop_constraint(
            "fk_cards_back_card_id",
            "cards",
            type_="foreignkey",
        )

        op.drop_column("cards", "back_card_id")


def downgrade():

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


    bind = op.get_bind()

    # Best-effort only: a card that ended up with more than one
    # distinct linked back (the exact scenario this migration exists
    # to support) can't be represented by the old one-per-card
    # column, so this just takes one of them.
    bind.execute(
        sa.text(
            """
            UPDATE cards
            SET back_card_id = sub.back_card_id
            FROM (
                SELECT DISTINCT ON (card_id) card_id, back_card_id
                FROM inventory
                WHERE back_card_id IS NOT NULL
                ORDER BY card_id, id
            ) AS sub
            WHERE cards.id = sub.card_id
            """
        )
    )


    op.drop_index("uq_inventory_linked", table_name="inventory")

    op.drop_index("uq_inventory_unlinked", table_name="inventory")

    op.create_unique_constraint(
        "uq_inventory_card_finish_treatment",
        "inventory",
        ["card_id", "finish", "treatment"],
    )

    op.drop_constraint(
        "fk_inventory_back_card_id",
        "inventory",
        type_="foreignkey",
    )

    op.drop_column("inventory", "back_card_id")
