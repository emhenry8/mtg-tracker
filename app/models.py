from sqlalchemy import (
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
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
    )


    __table_args__ = (

        UniqueConstraint(
            "card_id",
            "finish",
            "treatment",
            name="uq_inventory_card_finish_treatment",
        ),

    )
