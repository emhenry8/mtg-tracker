from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)

from sqlalchemy.orm import relationship

from .database import Base


class Card(Base):

    __tablename__ = "cards"


    id = Column(
        Integer,
        primary_key=True,
    )


    scryfall_id = Column(
        String(100),
        unique=True,
        nullable=True,
    )


    name = Column(
        String(255),
        nullable=False,
        index=True,
    )


    set_code = Column(
        String(10),
        nullable=False,
        index=True,
    )


    set_name = Column(
        String(255),
        nullable=True,
        index=True,
    )


    collector_number = Column(
        String(50),
        nullable=False,
        index=True,
    )


    type_line = Column(
        String(500),
        nullable=True,
        index=True,
    )


    rarity = Column(
        String(50),
        nullable=True,
        index=True,
    )


    mana_cost = Column(
        String(255),
        nullable=True,
    )


    oracle_text = Column(
        Text,
        nullable=True,
    )


    colors = Column(
        String(100),
        nullable=True,
    )


    # Comma-separated colors this card can add to your mana pool (lands,
    # mana rocks, dorks, etc.) — Scryfall's "produced_mana" field.
    produced_mana = Column(
        String(100),
        nullable=True,
    )


    image_url = Column(
        String(1000),
        nullable=True,
    )


    cmc = Column(
        Float,
        nullable=True,
    )


    price_usd = Column(
        Numeric(12, 2),
        nullable=True,
    )


    price_usd_foil = Column(
        Numeric(12, 2),
        nullable=True,
    )


    price_usd_etched = Column(
        Numeric(12, 2),
        nullable=True,
    )


    price_updated_at = Column(
        DateTime,
        nullable=True,
    )


    # JSON-encoded Scryfall "legalities" dict, e.g. {"standard": "legal", ...}.
    legalities = Column(
        Text,
        nullable=True,
    )


    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )


    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


    inventory_entries = relationship(
        "Inventory",
        back_populates="card",
        cascade="all, delete-orphan",
        foreign_keys="Inventory.card_id",
    )


    __table_args__ = (

        UniqueConstraint(
            "set_code",
            "collector_number",
            name="uq_card_set_collector",
        ),

    )


class Inventory(Base):

    __tablename__ = "inventory"


    id = Column(
        Integer,
        primary_key=True,
    )


    card_id = Column(
        Integer,
        ForeignKey(
            "cards.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )


    finish = Column(
        String(100),
        nullable=False,
        default="normal",
    )


    treatment = Column(
        String(255),
        nullable=False,
        default="regular",
    )


    quantity = Column(
        Integer,
        nullable=False,
        default=1,
    )


    # Points at the Card physically printed on the other side of this
    # specific batch of copies (e.g. a double-sided token's back
    # face). This lives on Inventory rather than Card because the
    # same card number can legitimately pair with different backs
    # across different physical print runs (WotC reuses generic
    # filler tokens — "Human," "Zombie," "Villain," etc. — as the back
    # of many unrelated front designs across different products), so
    # pairing is a property of a specific pile of physical cards, not
    # of the card's catalog identity. Scryfall only links faces
    # together for the rare "double_faced_token" layout; every other
    # back-to-back token pair has no API-visible relationship, so this
    # is set by hand.
    back_card_id = Column(
        Integer,
        ForeignKey(
            "cards.id",
            ondelete="SET NULL",
        ),
        nullable=True,
        index=True,
    )


    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )


    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


    card = relationship(
        "Card",
        back_populates="inventory_entries",
        foreign_keys=[card_id],
    )


    back_card = relationship(
        "Card",
        foreign_keys=[back_card_id],
    )


    __table_args__ = (

        # One row per (card, finish, treatment) among *unlinked*
        # batches (back_card_id IS NULL) — same rule as always for a
        # plain card or a token nobody's paired yet.
        Index(
            "uq_inventory_unlinked",
            "card_id",
            "finish",
            "treatment",
            unique=True,
            postgresql_where=text("back_card_id IS NULL"),
        ),

        # One row per (card, finish, treatment, back_card) among
        # *linked* batches — lets the same card number carry separate
        # rows for each distinct back it's actually been paired with.
        Index(
            "uq_inventory_linked",
            "card_id",
            "finish",
            "treatment",
            "back_card_id",
            unique=True,
            postgresql_where=text("back_card_id IS NOT NULL"),
        ),

    )


class Deck(Base):

    __tablename__ = "decks"


    id = Column(
        Integer,
        primary_key=True,
    )


    name = Column(
        String(255),
        nullable=False,
    )


    notes = Column(
        Text,
        nullable=True,
    )


    created_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )


    updated_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


    cards = relationship(
        "DeckCard",
        back_populates="deck",
        cascade="all, delete-orphan",
    )


class DeckCard(Base):

    __tablename__ = "deck_cards"


    id = Column(
        Integer,
        primary_key=True,
    )


    deck_id = Column(
        Integer,
        ForeignKey(
            "decks.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )


    card_id = Column(
        Integer,
        ForeignKey(
            "cards.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )


    section = Column(
        String(20),
        nullable=False,
        default="mainboard",
    )


    quantity = Column(
        Integer,
        nullable=False,
        default=1,
    )


    deck = relationship(
        "Deck",
        back_populates="cards",
    )


    card = relationship(
        "Card",
    )


    __table_args__ = (

        UniqueConstraint(
            "deck_id",
            "card_id",
            "section",
            name="uq_deck_card_section",
        ),

    )


class CollectionValueSnapshot(Base):

    __tablename__ = "collection_value_snapshots"


    id = Column(
        Integer,
        primary_key=True,
    )


    captured_at = Column(
        DateTime,
        nullable=False,
        server_default=func.now(),
    )


    total_cards = Column(
        Integer,
        nullable=False,
    )


    total_value = Column(
        Numeric(12, 2),
        nullable=False,
    )
