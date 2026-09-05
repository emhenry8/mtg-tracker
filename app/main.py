import asyncio
import csv
import json
import os
import random
import re

import requests

from collections import Counter
from contextlib import asynccontextmanager
from decimal import Decimal
from io import StringIO
from pathlib import Path
from typing import List
from urllib.parse import quote

from fastapi import (
    Depends,
    FastAPI,
    Form,
    Request,
)

from fastapi.responses import (
    JSONResponse,
    RedirectResponse,
    Response,
)

from fastapi.templating import Jinja2Templates

from sqlalchemy import (
    case,
    cast,
    func,
    Integer,
    nullsfirst,
    nullslast,
    or_,
)

from sqlalchemy.orm import (
    aliased,
    Session,
)

from .database import get_db, SessionLocal

from .models import (
    Card,
    CollectionValueSnapshot,
    Deck,
    DeckCard,
    Inventory,
)

from .scryfall import (
    get_variant_price,
    get_card_by_set_and_number,
    extract_card_data,
    refresh_card_data,
)


WEEKLY_REFRESH_INTERVAL_SECONDS = 7 * 24 * 60 * 60


async def weekly_price_refresh_loop():
    """Refresh every card's Scryfall price and record a collection
    value snapshot once a week, so the value-history chart fills in on
    its own without anyone having to click "Refresh Prices"."""

    while True:

        await asyncio.sleep(WEEKLY_REFRESH_INTERVAL_SECONDS)

        db = SessionLocal()

        try:

            await asyncio.to_thread(
                refresh_prices_and_snapshot,
                db,
            )

        except Exception:

            # A Scryfall hiccup shouldn't kill the weekly loop —
            # just try again next week.
            pass

        finally:

            db.close()


@asynccontextmanager
async def lifespan(app: FastAPI):

    # Database schema migrations are now handled
    # by Alembic before FastAPI starts.

    task = asyncio.create_task(weekly_price_refresh_loop())

    yield

    task.cancel()


app = FastAPI(
    title="MTG Collection Tracker",
    lifespan=lifespan,
)


COLOR_OPTIONS = [
    ("W", "White"),
    ("U", "Blue"),
    ("B", "Black"),
    ("R", "Red"),
    ("G", "Green"),
    ("colorless", "Colorless"),
]

COLOR_NAMES = dict(COLOR_OPTIONS)


# Moxfield's collection CSV importer matches columns by name (order
# doesn't matter), and expects exactly this header set:
# https://gist.github.com/Jerakin/24be913c6106546136c45d1d028f9af9
MOXFIELD_CSV_HEADERS = [
    "Count",
    "Tradelist Count",
    "Name",
    "Edition",
    "Condition",
    "Language",
    "Foil",
    "Tags",
    "Last Modified",
    "Collector Number",
    "Alter",
    "Proxy",
    "Purchase Price",
]


# ManaBox's CSV importer ("Settings > Import from CSV") matches columns
# by name and expects exactly this header set:
# https://help.manabox.app/csv-import
MANABOX_CSV_HEADERS = [
    "Folder Name",
    "Name",
    "Set code",
    "Set name",
    "Collector number",
    "Foil",
    "Rarity",
    "Quantity",
    "ManaBox ID",
    "Scryfall ID",
    "Purchase price",
    "Misprint",
    "Altered",
    "Condition",
    "Language",
    "Purchase price currency",
]


BASE_DIR = Path(__file__).resolve().parent


templates = Jinja2Templates(
    directory=str(
        BASE_DIR / "templates"
    )
)


def render_template(
    request: Request,
    name: str,
    context: dict,
):

    return templates.TemplateResponse(
        request=request,
        name=name,
        context=context,
    )


def inventory_price(
    row,
):

    price = get_variant_price(
        row.card,
        row.finish,
    )

    if price is None:

        return Decimal("0.00")

    return Decimal(price)


@app.get("/")
def home(
    request: Request,
    error: str = None,
    db: Session = Depends(get_db),
):

    total_cards = (
        db.query(
            func.coalesce(
                func.sum(
                    Inventory.quantity
                ),
                0,
            )
        )
        .scalar()
    )


    unique_inventory_entries = (
        db.query(Inventory)
        .count()
    )


    return render_template(
        request,
        "index.html",
        {
            "total_cards":
                total_cards,

            "unique_inventory_entries":
                unique_inventory_entries,

            "error":
                error,
        },
    )


def get_or_create_card(
    db: Session,
    set_code: str,
    collector_number: str,
):
    """Look up a card by set + collector number, fetching it from
    Scryfall and persisting it if we don't have it yet.

    Returns the Card, or None if Scryfall has no such printing. Lets
    network/HTTP errors from Scryfall propagate — callers decide how
    to report those.
    """

    set_code = (
        set_code
        .upper()
        .strip()
    )

    collector_number = (
        collector_number
        .strip()
    )


    card = (
        db.query(Card)
        .filter(
            Card.set_code ==
                set_code,

            Card.collector_number ==
                collector_number,
        )
        .first()
    )


    if card:

        return card


    scryfall_data = (
        get_card_by_set_and_number(
            set_code,
            collector_number,
        )
    )


    if not scryfall_data:

        return None


    card_data = (
        extract_card_data(
            scryfall_data
        )
    )


    card = Card(
        **card_data
    )


    db.add(card)

    db.commit()

    db.refresh(card)


    return card


def _collector_sort_key(collector_number: str):
    """Numeric collector numbers sort as numbers ("2" < "10"); anything
    that doesn't parse (variant suffixes like "4a") falls back to a
    string compare, ordered after every plain-numeric one."""

    try:

        return (0, int(collector_number))

    except (TypeError, ValueError):

        return (1, collector_number or "")


def link_back_face(
    db: Session,
    inventory_id: int,
    other_card_id: int,
):
    """Link a specific batch of inventory to a back-face card.

    Pairing lives on the Inventory row, not the Card, because the same
    card number can legitimately pair with different backs across
    different physical print runs (WotC reuses generic filler tokens
    as the back of many unrelated front designs). The pair's quantity
    always ends up on one row, keyed by the lower collector number
    (the "primary" side) regardless of which one was passed in as
    front/back — linking 2->6 and linking 6->2 land on the same row.

    Any inventory already sitting unlinked on either side (same
    finish/treatment) is folded in too, on the assumption that an
    unlinked batch is unattributed and fair game the first time a
    specific pairing is established for it. A batch already linked to
    a *different* partner is left alone — that's a distinct, already-
    known physical pairing and must never be merged into this one.
    """

    source = (
        db.query(Inventory)
        .filter(Inventory.id == inventory_id)
        .first()
    )

    if not source:

        return


    other_card = (
        db.query(Card)
        .filter(Card.id == other_card_id)
        .first()
    )

    if not other_card or other_card.id == source.card_id:

        return


    primary, secondary = sorted(
        (source.card, other_card),
        key=lambda card: _collector_sort_key(
            card.collector_number
        ),
    )

    finish = source.finish

    treatment = source.treatment


    existing_linked = (
        db.query(Inventory)
        .filter(
            Inventory.card_id == primary.id,
            Inventory.back_card_id == secondary.id,
            Inventory.finish == finish,
            Inventory.treatment == treatment,
            Inventory.id != source.id,
        )
        .first()
    )

    primary_unlinked = (
        db.query(Inventory)
        .filter(
            Inventory.card_id == primary.id,
            Inventory.back_card_id.is_(None),
            Inventory.finish == finish,
            Inventory.treatment == treatment,
            Inventory.id != source.id,
        )
        .first()
    )

    secondary_unlinked = (
        db.query(Inventory)
        .filter(
            Inventory.card_id == secondary.id,
            Inventory.back_card_id.is_(None),
            Inventory.finish == finish,
            Inventory.treatment == treatment,
            Inventory.id != source.id,
        )
        .first()
    )

    extra_rows = [
        row
        for row in (
            existing_linked, primary_unlinked, secondary_unlinked
        )
        if row is not None
    ]

    total_quantity = source.quantity + sum(
        row.quantity for row in extra_rows
    )


    keeper = existing_linked or (
        source if source.card_id == primary.id else None
    )

    rows_to_delete = [
        row
        for row in ([source] + extra_rows)
        if row is not keeper
    ]

    for row in rows_to_delete:

        db.delete(row)

    db.flush()


    if keeper is None:

        keeper = Inventory(
            card_id=primary.id,
            back_card_id=secondary.id,
            finish=finish,
            treatment=treatment,
            quantity=total_quantity,
        )

        db.add(keeper)

    else:

        keeper.card_id = primary.id

        keeper.back_card_id = secondary.id

        keeper.quantity = total_quantity


    db.commit()


def unlink_back_face(
    db: Session,
    inventory_id: int,
):
    """Clear the back-face link on one specific inventory row.

    Does not attempt to un-merge quantity that was folded in when the
    link was created — there's no way to know how to split it back
    up — so whatever quantity currently sits on this row stays here.
    If unlinking would collide with another already-unlinked row for
    the same card/finish/treatment, folds into that row instead of
    creating a duplicate.
    """

    row = (
        db.query(Inventory)
        .filter(Inventory.id == inventory_id)
        .first()
    )

    if not row or not row.back_card_id:

        return


    existing_unlinked = (
        db.query(Inventory)
        .filter(
            Inventory.card_id == row.card_id,
            Inventory.back_card_id.is_(None),
            Inventory.finish == row.finish,
            Inventory.treatment == row.treatment,
            Inventory.id != row.id,
        )
        .first()
    )

    if existing_unlinked:

        existing_unlinked.quantity += row.quantity

        db.delete(row)

    else:

        row.back_card_id = None

    db.commit()


def _normalize_finish(finish: str) -> str:

    return (finish or "").strip().lower() or "normal"


def _normalize_treatment(treatment: str) -> str:

    return (treatment or "").strip().lower() or "regular"


def find_unlinked_inventory_row(
    db: Session,
    card_id: int,
    finish: str,
    treatment: str,
):
    """The one (guaranteed-unique, per the partial index) inventory
    row for this card/finish/treatment that isn't linked to a back
    face yet — i.e. the row a plain add_card_to_collection call just
    touched, before any back-face linking is applied to it.
    """

    return (
        db.query(Inventory)
        .filter(
            Inventory.card_id == card_id,
            Inventory.finish == finish,
            Inventory.treatment == treatment,
            Inventory.back_card_id.is_(None),
        )
        .first()
    )


def add_card_to_collection(
    db: Session,
    set_code: str,
    collector_number: str,
    finish: str,
    treatment: str,
    quantity: int,
):
    """Look up (or fetch from Scryfall) a card and add it to inventory.

    Returns (success, message) — message is the card name on success,
    or an error description on failure. Shared by the single-card and
    bulk-add endpoints.
    """

    set_code = (
        set_code
        .upper()
        .strip()
    )

    collector_number = (
        collector_number
        .strip()
    )

    finish = _normalize_finish(finish)

    treatment = _normalize_treatment(treatment)


    if quantity <= 0:

        return False, "Quantity must be at least 1"


    try:

        card = get_or_create_card(
            db,
            set_code,
            collector_number,
        )

    except Exception:

        return False, "Could not contact Scryfall"


    if not card:

        return (
            False,
            f"Card not found: {set_code} #{collector_number}",
        )


    inventory = find_unlinked_inventory_row(
        db, card.id, finish, treatment
    )


    if inventory:

        inventory.quantity += quantity

    else:

        inventory = Inventory(

            card_id=card.id,

            finish=finish,

            treatment=treatment,

            quantity=quantity,

        )

        db.add(inventory)


    db.commit()


    return True, card.name


@app.post("/add")
def add_card(
    request: Request,

    set_code: str = Form(...),

    collector_number: str = Form(...),

    finish: str = Form(...),

    treatment: str = Form(...),

    quantity: int = Form(...),

    back_set_code: str = Form(""),

    back_collector_number: str = Form(""),

    db: Session = Depends(get_db),
):

    success, message = add_card_to_collection(
        db,
        set_code,
        collector_number,
        finish,
        treatment,
        quantity,
    )


    if success and back_collector_number.strip():

        front_card = get_or_create_card(
            db, set_code, collector_number
        )

        effective_back_set_code = (
            back_set_code.strip() or set_code
        ).upper().strip()

        back_card = get_or_create_card(
            db,
            effective_back_set_code,
            back_collector_number,
        )

        if not back_card:

            success = False

            message = (
                f"Added the card, but back face "
                f"{effective_back_set_code} #{back_collector_number} "
                f"was not found — no link created"
            )

        elif front_card:

            source_row = find_unlinked_inventory_row(
                db,
                front_card.id,
                _normalize_finish(finish),
                _normalize_treatment(treatment),
            )

            if source_row:

                link_back_face(
                    db, source_row.id, back_card.id
                )


    if not success:

        return RedirectResponse(
            url=f"/?error={quote(message)}",
            status_code=303,
        )


    return RedirectResponse(
        url="/collection",
        status_code=303,
    )


@app.get("/bulk-add")
def bulk_add_form(
    request: Request,
    db: Session = Depends(get_db),
):

    decks = (
        db.query(Deck)
        .order_by(Deck.name)
        .all()
    )

    return render_template(
        request,
        "bulk_add.html",
        {
            "results": None,
            "decks": decks,
        },
    )


@app.post("/bulk-add")
def bulk_add(
    request: Request,

    set_code: str = Form(...),

    treatment: str = Form(...),

    collector_number: List[str] = Form(default=[]),

    row_finish: List[str] = Form(default=[]),

    row_set_code: List[str] = Form(default=[]),

    row_back_number: List[str] = Form(default=[]),

    row_back_set_code: List[str] = Form(default=[]),

    quantity: List[str] = Form(default=[]),

    deck_choice: str = Form(""),

    new_deck_name: str = Form(""),

    deck_section: str = Form("mainboard"),

    db: Session = Depends(get_db),
):

    deck = None

    if deck_choice == "__new__":

        deck = get_or_create_deck_by_name(db, new_deck_name)

    elif deck_choice:

        deck = (
            db.query(Deck)
            .filter(Deck.id == int(deck_choice))
            .first()
        )

    if deck_section not in DECK_SECTIONS:

        deck_section = "mainboard"


    results = []

    for (
        number,
        qty_raw,
        row_finish_val,
        row_set_code_val,
        row_back_val,
        row_back_set_val,
    ) in zip(
        collector_number,
        quantity,
        row_finish,
        row_set_code,
        row_back_number,
        row_back_set_code,
    ):

        number = number.strip()

        if not number:

            continue


        effective_set_code = (
            row_set_code_val.strip()
            or set_code
        ).upper().strip()

        effective_finish = (
            row_finish_val.strip().lower()
            or "normal"
        )


        try:

            qty = int(
                (qty_raw or "").strip()
                or "1"
            )

        except ValueError:

            results.append({
                "collector_number": number,
                "set_code": effective_set_code,
                "finish": effective_finish,
                "success": False,
                "message": "Quantity must be a number",
            })

            continue


        success, message = add_card_to_collection(
            db,
            effective_set_code,
            number,
            effective_finish,
            treatment,
            qty,
        )

        card = None

        back_number = row_back_val.strip()

        back_set_given = row_back_set_val.strip()

        if success and back_set_given and not back_number:

            # A back set code with no back number can't do anything —
            # almost always means the number landed in the wrong
            # column. Flag it instead of silently adding a plain,
            # unlinked quantity as if nothing was asked for.
            message = (
                f"{message} (back set '{back_set_given}' given but "
                f"Back # was blank — no link created; check you put "
                f"the collector number in Back #, not Back Set)"
            )

        if success and (deck or back_number):

            card = get_or_create_card(db, effective_set_code, number)

        if success and deck:

            add_card_quantity_to_deck(
                db,
                deck.id,
                card.id,
                deck_section,
                qty,
            )

            message = f"{message} (added to {deck.name})"

        if success and back_number and card:

            effective_back_set_code = (
                back_set_given
                or effective_set_code
            ).upper().strip()

            back_card = get_or_create_card(
                db, effective_back_set_code, back_number
            )

            if not back_card:

                message = (
                    f"{message} (back face {effective_back_set_code} "
                    f"#{back_number} not found — no link created)"
                )

            else:

                source_row = find_unlinked_inventory_row(
                    db,
                    card.id,
                    _normalize_finish(effective_finish),
                    _normalize_treatment(treatment),
                )

                if source_row:

                    link_back_face(db, source_row.id, back_card.id)

                    message = f"{message} (linked back face #{back_number})"

        results.append({
            "collector_number": number,
            "set_code": effective_set_code,
            "finish": effective_finish,
            "quantity": qty,
            "success": success,
            "message": message,
        })


    decks = (
        db.query(Deck)
        .order_by(Deck.name)
        .all()
    )

    return render_template(
        request,
        "bulk_add.html",
        {
            "results": results,
            "decks": decks,
            "last_set_code": set_code.upper().strip(),
            "last_treatment": treatment,
            "deck_link": deck,
        },
    )


def build_collection_query(
    db: Session,
    search: str = "",
    set_code: str = "",
    card_type: str = "",
    color: str = "",
    rarity: str = "",
    finish: str = "",
    treatment: str = "",
    has_back: bool = False,
    sort: str = "name",
    direction: str = "asc",
):
    """Build the filtered/sorted Inventory query shared by the
    collection page and the CSV export, so both stay in sync."""

    BackCard = aliased(Card)

    query = (
        db.query(Inventory)
        .join(Card, Inventory.card_id == Card.id)
        .outerjoin(
            BackCard,
            Inventory.back_card_id == BackCard.id,
        )
    )


    if search:

        like = f"%{search.strip()}%"

        query = query.filter(
            or_(
                Card.name.ilike(like),
                BackCard.name.ilike(like),
            )
        )


    if has_back:

        query = query.filter(
            Inventory.back_card_id.isnot(None)
        )


    if set_code:

        query = query.filter(
            Card.set_code ==
                set_code
        )


    if card_type:

        query = query.filter(
            Card.type_line.ilike(
                f"%{card_type}%"
            )
        )


    if color == "colorless":

        query = query.filter(
            (Card.colors == "")
            | Card.colors.is_(None)
        )

    elif color:

        query = query.filter(
            func.concat(
                ",",
                Card.colors,
                ",",
            ).ilike(
                f"%,{color},%"
            )
        )


    if rarity:

        query = query.filter(
            Card.rarity ==
                rarity
        )


    if finish:

        query = query.filter(
            Inventory.finish ==
                finish
        )


    if treatment:

        query = query.filter(
            Inventory.treatment ==
                treatment
        )


    price_expression = case(

        (
            func.lower(
                Inventory.finish
            ) == "foil",

            func.coalesce(
                Card.price_usd_foil,
                Card.price_usd,
            ),
        ),

        (
            func.lower(
                Inventory.finish
            ) == "etched",

            func.coalesce(
                Card.price_usd_etched,
                Card.price_usd_foil,
                Card.price_usd,
            ),
        ),

        else_=Card.price_usd,
    )


    if sort == "price":

        order_column = (
            price_expression
        )

    elif sort == "set":

        order_column = (
            Card.set_code
        )

    elif sort == "rarity":

        order_column = (
            Card.rarity
        )

    elif sort == "date_added":

        order_column = (
            Inventory.created_at
        )

    elif sort == "updated":

        order_column = (
            Inventory.updated_at
        )

    else:

        order_column = (
            Card.name
        )


    if direction == "desc":

        query = query.order_by(
            nullslast(order_column.desc())
            if sort == "price"
            else order_column.desc()
        )

    else:

        query = query.order_by(
            nullslast(order_column.asc())
            if sort == "price"
            else order_column.asc()
        )


    if sort == "set":

        # A front card linked to more than one distinct back face
        # gets one row per pairing, so break ties within the same
        # (set, front number) by back-face number — numerically,
        # since collector numbers are strings ("10" would otherwise
        # sort before "2") — unpaired rows first, then each pairing
        # in ascending back-number order.
        back_number_numeric = case(
            (
                BackCard.collector_number.op("~")(r"^\d+$"),
                cast(BackCard.collector_number, Integer),
            ),
            else_=None,
        )

        query = query.order_by(
            Card.set_code.asc(),
            Card.collector_number.asc(),
            nullsfirst(back_number_numeric.asc()),
        )


    return query


def get_collection_filter_options(
    db: Session,
):
    """Distinct filter dropdown values shared by the collection page
    and the deck builder's "add from collection" panel."""

    set_codes = [
        row[0]

        for row in (
            db.query(
                Card.set_code
            )
            .distinct()
            .order_by(
                Card.set_code
            )
            .all()
        )
    ]


    rarities = [
        row[0]

        for row in (
            db.query(
                Card.rarity
            )
            .filter(
                Card.rarity.isnot(None)
            )
            .distinct()
            .order_by(
                Card.rarity
            )
            .all()
        )
    ]


    finishes = [
        row[0]

        for row in (
            db.query(
                Inventory.finish
            )
            .distinct()
            .order_by(
                Inventory.finish
            )
            .all()
        )
    ]


    treatments = [
        row[0]

        for row in (
            db.query(
                Inventory.treatment
            )
            .distinct()
            .order_by(
                Inventory.treatment
            )
            .all()
        )
    ]


    return {

        "set_codes":
            set_codes,

        "rarities":
            rarities,

        "finishes":
            finishes,

        "treatments":
            treatments,

    }


@app.get("/collection")
def collection(
    request: Request,

    search: str = "",

    set_code: str = "",

    card_type: str = "",

    color: str = "",

    rarity: str = "",

    finish: str = "",

    treatment: str = "",

    has_back: bool = False,

    view: str = "gallery",

    sort: str = "date_added",

    direction: str = "desc",

    db: Session = Depends(get_db),
):

    if view not in ("card", "list", "gallery"):

        view = "card"


    inventory_rows = (
        build_collection_query(
            db,
            search=search,
            set_code=set_code,
            card_type=card_type,
            color=color,
            rarity=rarity,
            finish=finish,
            treatment=treatment,
            has_back=has_back,
            sort=sort,
            direction=direction,
        )
        .all()
    )


    total_cards = sum(
        row.quantity
        for row in inventory_rows
    )


    total_value = sum(

        inventory_price(row)
        * row.quantity

        for row in inventory_rows

    )


    filter_options = get_collection_filter_options(db)


    return render_template(

        request,

        "collection.html",

        {
            "inventory_rows":
                inventory_rows,

            "total_cards":
                total_cards,

            "total_value":
                total_value,

            "set_codes":
                filter_options["set_codes"],

            "rarities":
                filter_options["rarities"],

            "colors":
                COLOR_OPTIONS,

            "finishes":
                filter_options["finishes"],

            "treatments":
                filter_options["treatments"],

            "filters": {

                "search":
                    search,

                "set_code":
                    set_code,

                "card_type":
                    card_type,

                "color":
                    color,

                "rarity":
                    rarity,

                "finish":
                    finish,

                "treatment":
                    treatment,

                "has_back":
                    has_back,

                "view":
                    view,

                "sort":
                    sort,

                "direction":
                    direction,

            },

            "inventory_price":
                inventory_price,
        },
    )


@app.get("/collection/export.csv")
def export_collection_csv(
    search: str = "",

    set_code: str = "",

    card_type: str = "",

    color: str = "",

    rarity: str = "",

    finish: str = "",

    treatment: str = "",

    sort: str = "name",

    direction: str = "asc",

    db: Session = Depends(get_db),
):
    """Export the (optionally filtered) collection as a CSV ready to
    import into Moxfield's collection ("Import > CSV") page.

    We don't track physical condition or language, so those columns
    are filled with the common defaults (Near Mint / English) — edit
    them in Moxfield afterward if any of your copies differ.
    """

    inventory_rows = (
        build_collection_query(
            db,
            search=search,
            set_code=set_code,
            card_type=card_type,
            color=color,
            rarity=rarity,
            finish=finish,
            treatment=treatment,
            sort=sort,
            direction=direction,
        )
        .all()
    )


    buffer = StringIO()

    writer = csv.writer(buffer)

    writer.writerow(MOXFIELD_CSV_HEADERS)


    for row in inventory_rows:

        card = row.card

        finish_value = (
            row.finish
            if row.finish in ("foil", "etched")
            else ""
        )

        tags_value = (
            row.treatment
            if row.treatment
                and row.treatment != "regular"
            else ""
        )

        last_modified = (
            row.updated_at.isoformat()
            if row.updated_at
            else ""
        )

        writer.writerow([
            row.quantity,
            "",
            card.name,
            card.set_name or "",
            "NM",
            "English",
            finish_value,
            tags_value,
            last_modified,
            card.collector_number,
            "",
            "",
            "",
        ])


    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                'attachment; filename="moxfield_collection_import.csv"',
        },
    )


@app.get("/collection/export/manabox.csv")
def export_collection_manabox_csv(
    search: str = "",

    set_code: str = "",

    card_type: str = "",

    color: str = "",

    rarity: str = "",

    finish: str = "",

    treatment: str = "",

    sort: str = "name",

    direction: str = "asc",

    db: Session = Depends(get_db),
):
    """Export the (optionally filtered) collection as a CSV ready to
    import into ManaBox ("Settings > Import from CSV").

    We don't track physical condition, language, or purchase price, so
    those columns are filled with common defaults (Near Mint / English)
    — edit them in ManaBox afterward if any of your copies differ.
    """

    inventory_rows = (
        build_collection_query(
            db,
            search=search,
            set_code=set_code,
            card_type=card_type,
            color=color,
            rarity=rarity,
            finish=finish,
            treatment=treatment,
            sort=sort,
            direction=direction,
        )
        .all()
    )


    buffer = StringIO()

    writer = csv.writer(buffer)

    writer.writerow(MANABOX_CSV_HEADERS)


    for row in inventory_rows:

        card = row.card

        foil_value = (
            row.finish
            if row.finish in ("foil", "etched")
            else "normal"
        )

        writer.writerow([
            "",
            card.name,
            card.set_code,
            card.set_name or "",
            card.collector_number,
            foil_value,
            card.rarity or "",
            row.quantity,
            "",
            card.scryfall_id or "",
            "",
            "false",
            "false",
            "near_mint",
            "en",
            "USD",
        ])


    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition":
                'attachment; filename="manabox_collection_import.csv"',
        },
    )


DECK_SECTIONS = ("mainboard", "sideboard")


def owned_quantity_for_card(
    db: Session,
    card_id: int,
):
    """Total physical copies that have this card printed on them.

    Counts rows where it's the front (Inventory.card_id) as well as
    rows where it's someone else's linked back face
    (Inventory.back_card_id) — a linked pair's quantity always lives
    on the primary side's row, so this makes deck-building work the
    same regardless of which face's Card id you're adding.
    """

    return (
        db.query(
            func.coalesce(
                func.sum(
                    Inventory.quantity
                ),
                0,
            )
        )
        .filter(
            or_(
                Inventory.card_id == card_id,
                Inventory.back_card_id == card_id,
            )
        )
        .scalar()
    )


def slugify_deck_name(
    name: str,
):
    slug = (
        re.sub(
            r"[^A-Za-z0-9]+",
            "_",
            name.strip(),
        )
        .strip("_")
        .lower()
    )

    return slug or "deck"


@app.get("/decks")
def list_decks(
    request: Request,
    exported: int = None,
    db: Session = Depends(get_db),
):

    decks = (
        db.query(Deck)
        .order_by(
            Deck.updated_at.desc()
        )
        .all()
    )


    deck_rows = []

    for deck in decks:

        mainboard_count = sum(
            deck_card.quantity
            for deck_card in deck.cards
            if deck_card.section == "mainboard"
        )

        sideboard_count = sum(
            deck_card.quantity
            for deck_card in deck.cards
            if deck_card.section == "sideboard"
        )

        deck_rows.append({
            "deck": deck,
            "mainboard_count": mainboard_count,
            "sideboard_count": sideboard_count,
        })


    return render_template(
        request,
        "decks.html",
        {
            "deck_rows": deck_rows,
            "exported": exported,
        },
    )


@app.post("/decks")
def create_deck(
    name: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):

    deck = Deck(
        name=name.strip() or "Untitled Deck",
        notes=notes.strip() or None,
    )

    db.add(deck)

    db.commit()

    db.refresh(deck)


    return RedirectResponse(
        url=f"/decks/{deck.id}",
        status_code=303,
    )


@app.get("/decks/import")
def deck_import_form(
    request: Request,
):

    return render_template(
        request,
        "deck_import.html",
        {
            "results": None,
        },
    )


# Matches the MTGA/Arena-style decklist line format that Moxfield,
# Archidekt, and ManaBox all export/import, e.g.:
#   4 Lightning Bolt (M11) 146
#   1 Sol Ring (C21) 263 *F*
DECK_LINE_RE = re.compile(
    r"^(\d+)x?\s+(.+?)\s*\(([A-Za-z0-9]{2,6})\)\s*(\S+?)\s*(?:\*[A-Za-z]+\*)?$"
)

SECTION_HEADER_RE = re.compile(
    r"^(deck|mainboard|sideboard|commander)\s*:?\s*$",
    re.IGNORECASE,
)


@app.post("/decks/import")
def deck_import(
    request: Request,
    name: str = Form(...),
    decklist: str = Form(...),
    db: Session = Depends(get_db),
):

    deck = Deck(
        name=name.strip() or "Imported Deck",
    )

    db.add(deck)

    db.commit()

    db.refresh(deck)


    section = "mainboard"

    results = []

    for raw_line in decklist.splitlines():

        line = raw_line.strip()

        if not line:

            continue


        header_match = (
            SECTION_HEADER_RE.match(line)
        )

        if header_match:

            header = header_match.group(1).lower()

            section = (
                "sideboard"
                if header == "sideboard"
                else "mainboard"
            )

            continue


        match = DECK_LINE_RE.match(line)

        if not match:

            results.append({
                "line": line,
                "success": False,
                "message": "Could not parse line",
            })

            continue


        quantity_str, _name_hint, set_code, collector_number = (
            match.groups()
        )

        quantity = int(quantity_str)


        try:

            card = get_or_create_card(
                db,
                set_code,
                collector_number,
            )

        except Exception:

            results.append({
                "line": line,
                "success": False,
                "message": "Could not contact Scryfall",
            })

            continue


        if not card:

            results.append({
                "line": line,
                "success": False,
                "message":
                    f"Card not found: {set_code} #{collector_number}",
            })

            continue


        deck_card = (
            db.query(DeckCard)
            .filter(
                DeckCard.deck_id == deck.id,
                DeckCard.card_id == card.id,
                DeckCard.section == section,
            )
            .first()
        )

        if deck_card:

            deck_card.quantity += quantity

        else:

            db.add(
                DeckCard(
                    deck_id=deck.id,
                    card_id=card.id,
                    section=section,
                    quantity=quantity,
                )
            )

        db.commit()

        results.append({
            "line": line,
            "success": True,
            "message": f"Added {quantity}x {card.name} to {section}",
        })


    if any(not result["success"] for result in results):

        return render_template(
            request,
            "deck_import.html",
            {
                "results": results,
                "deck_id": deck.id,
            },
        )


    return RedirectResponse(
        url=f"/decks/{deck.id}",
        status_code=303,
    )


# Order matters: a card's primary type for breakdown purposes is the
# first of these that appears in its type_line (e.g. an "Artifact
# Creature" counts as a Creature, a land with a type line like "Land —
# Gate" still counts as a Land).
DECK_TYPE_CATEGORIES = [
    "Land",
    "Creature",
    "Planeswalker",
    "Battle",
    "Instant",
    "Sorcery",
    "Artifact",
    "Enchantment",
]

CURVE_BUCKETS = ["0", "1", "2", "3", "4", "5", "6+"]

WUBRG = ("W", "U", "B", "R", "G")

MANA_SYMBOL_RE = re.compile(r"\{([^}]+)\}")


def parse_mana_pips(mana_cost):
    """Count colored mana symbols in a cost string like "{1}{R}{R}",
    weighting hybrid/Phyrexian symbols across the colors they can pay
    (e.g. {R/W} is 0.5 pips each of red and white; {R/P} is 1 red pip,
    since the Phyrexian option doesn't change what color the spell is).
    Generic and colorless ({2}, {X}, {C}) symbols contribute no pips.
    """

    pips = Counter()

    for symbol in MANA_SYMBOL_RE.findall(mana_cost or ""):

        colors_in_symbol = [
            part
            for part in symbol.split("/")
            if part in WUBRG
        ]

        if not colors_in_symbol:

            continue

        weight = 1 / len(colors_in_symbol)

        for color in colors_in_symbol:

            pips[color] += weight

    return pips


def categorize_card_type(type_line):

    type_line = type_line or ""

    return next(
        (
            option
            for option in DECK_TYPE_CATEGORIES
            if option in type_line
        ),
        "Other",
    )


MAX_COPIES_NONBASIC = 4


def card_standard_legality(card):
    """The Standard legality status Scryfall reported for this card
    the last time its data was refreshed, or None if we've never
    fetched it (e.g. it was added before this field existed)."""

    if not card.legalities:

        return None

    try:

        legalities = json.loads(card.legalities)

    except (TypeError, ValueError):

        return None

    return legalities.get("standard")


def check_standard_legality(deck_cards):
    """A soft, non-blocking check of the mainboard against Standard
    rules: banned/rotated-out cards and over-the-limit copy counts.
    Returns a dict with `warnings` (plain-language strings) and
    `unknown_count` (cards we don't have legality data for yet —
    running "Refresh Prices" on the Collection page backfills it).
    """

    warnings = []

    unknown_count = 0

    for deck_card in deck_cards:

        card = deck_card.card

        status = card_standard_legality(card)

        if status is None:

            unknown_count += 1

        elif status == "banned":

            warnings.append(f"{card.name} is banned in Standard")

        elif status != "legal":

            warnings.append(f"{card.name} is not currently legal in Standard")

        is_basic_land = "Basic" in (card.type_line or "")

        if not is_basic_land and deck_card.quantity > MAX_COPIES_NONBASIC:

            warnings.append(
                f"{deck_card.quantity}x {card.name} exceeds the "
                f"{MAX_COPIES_NONBASIC}-copy limit"
            )

    return {
        "warnings": warnings,
        "unknown_count": unknown_count,
    }


def check_mana_base(deck_cards):
    """A soft, non-blocking check that every color a spell needs is
    actually produced by something in the deck — lands, mana rocks,
    dorks, anything with Scryfall's "produced_mana" data. Doesn't
    weigh in on *how many* sources you have, just whether you have
    zero, which is the case where a card is flatly uncastable.
    """

    produced_colors = set()

    unknown_land_count = 0

    for deck_card in deck_cards:

        card = deck_card.card

        if card.produced_mana is None:

            if categorize_card_type(card.type_line) == "Land":

                unknown_land_count += 1

            continue

        produced_colors.update(
            color
            for color in card.produced_mana.split(",")
            if color
        )

    warnings = []

    # If some lands' produced colors are unknown, we can't confidently
    # claim a color is *missing* — one of those lands might supply it.
    # Rather than risk a false alarm, hold off on warnings until every
    # land has been checked (surfaced instead via unknown_land_count).
    if unknown_land_count == 0:

        for deck_card in deck_cards:

            card = deck_card.card

            if categorize_card_type(card.type_line) == "Land":

                continue

            # Check per mana symbol, not per overall card color — a
            # hybrid symbol like {U/R} only needs ONE of its colors,
            # so a card with card.colors "R,U" is castable off just
            # blue sources even with zero red in the deck.
            unpayable_symbols = []

            for symbol in MANA_SYMBOL_RE.findall(card.mana_cost or ""):

                colors_in_symbol = [
                    part
                    for part in symbol.split("/")
                    if part in WUBRG
                ]

                if colors_in_symbol and not (set(colors_in_symbol) & produced_colors):

                    unpayable_symbols.append(tuple(
                        sorted(colors_in_symbol, key=WUBRG.index)
                    ))

            if not unpayable_symbols:

                continue

            needs_text = " and ".join(
                " or ".join(COLOR_NAMES.get(color, color) for color in group)
                for group in sorted(set(unpayable_symbols))
            )

            warnings.append(
                f"{card.name} needs {needs_text} mana, which nothing in "
                f"your deck produces"
            )

    return {
        "warnings": warnings,
        "unknown_land_count": unknown_land_count,
    }


_TOKEN_COUNT_WORDS = (
    r"a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+"
)

_TOKEN_CREATE_RE = re.compile(
    rf"creates?\s+(?:up to \w+\s+)?(?:X\s+)?"
    rf"(?:{_TOKEN_COUNT_WORDS})\s+(.+?)\s+tokens?\b",
    re.IGNORECASE,
)


def find_potential_tokens(deck_cards):
    """Best-effort scan of each mainboard card's oracle text for
    token-creation effects, so you know what physical tokens to have
    on hand before you sit down to play — they don't belong in the
    60, but you'll want them nearby. Text matching against free-form
    Oracle wording, so treat this as a helpful checklist, not a
    guaranteed-complete one.
    """

    tokens = {}

    for deck_card in deck_cards:

        card = deck_card.card

        if not card.oracle_text:

            continue

        for line in card.oracle_text.splitlines():

            for match in _TOKEN_CREATE_RE.finditer(line):

                description = match.group(1).strip()

                # "a token that's a copy of ..." has no fixed type to
                # stock up on — skip it.
                if "copy of" in description.lower():

                    continue

                entry = tokens.setdefault(
                    description.lower(),
                    {
                        "description": description,
                        "cards": set(),
                    },
                )

                entry["cards"].add(card.name)

    return sorted(
        tokens.values(),
        key=lambda entry: entry["description"].lower(),
    )


def deck_card_price(card):
    """Best-available USD price for a card at its default (nonfoil)
    finish — DeckCard doesn't track finish, so fall back to whatever
    price Scryfall has for cards that are only printed as foil/etched.
    """

    price = (
        card.price_usd
        or card.price_usd_foil
        or card.price_usd_etched
    )

    return Decimal(price) if price is not None else Decimal("0.00")


def deck_section_cost(deck_cards):

    return sum(
        (
            deck_card_price(deck_card.card) * deck_card.quantity
            for deck_card in deck_cards
        ),
        Decimal("0.00"),
    )


def summarize_deck_composition(deck_cards):
    """Build a high-level breakdown of a deck section: card type mix,
    colored-mana-symbol (pip) weight, and mana curve. Only counts
    nonland cards toward pips and curve, since lands don't have a
    casting cost.
    """

    total = 0

    type_counts = {category: 0 for category in DECK_TYPE_CATEGORIES}

    type_counts["Other"] = 0

    pip_counts = {color: 0.0 for color in WUBRG}

    curve = {bucket: 0 for bucket in CURVE_BUCKETS}

    for deck_card in deck_cards:

        card = deck_card.card

        quantity = deck_card.quantity

        total += quantity

        category = categorize_card_type(card.type_line)

        type_counts[category] += quantity

        if category == "Land":

            continue

        for color, weight in parse_mana_pips(card.mana_cost).items():

            pip_counts[color] += weight * quantity

        cmc = card.cmc

        bucket = "6+" if cmc is None or cmc >= 6 else str(int(cmc))

        curve[bucket] += quantity

    land_count = type_counts["Land"]

    return {
        "total": total,
        "land_count": land_count,
        "nonland_count": total - land_count,
        "type_counts": type_counts,
        "pip_counts": pip_counts,
        "total_pips": sum(pip_counts.values()),
        "curve": curve,
    }


OPENING_HAND_SIZE = 7

HAND_SIM_TRIALS = 10000

# A 7-card hand with 2-5 lands is generally considered a reasonable
# keep; outside that range you're flooded or screwed more often than not.
KEEPABLE_LAND_RANGE = range(2, 6)


def build_deck_card_pool(deck_cards):
    """Flatten a deck section into one list entry per physical copy of
    each card, so sampling without replacement respects quantities —
    e.g. you can't draw a 5th copy of a card you only run 4 of, and
    once a copy is drawn it's gone for the rest of that hand."""

    pool = []

    for deck_card in deck_cards:

        pool.extend([deck_card.card] * deck_card.quantity)

    return pool


def draw_opening_hand(pool):

    return random.sample(pool, min(OPENING_HAND_SIZE, len(pool)))


def simulate_hand_stats(pool, trials=HAND_SIM_TRIALS):
    """Draw `trials` independent 7-card hands (each one sampled
    without replacement from the deck) and summarize the results."""

    hand_size = min(OPENING_HAND_SIZE, len(pool))

    if hand_size == 0:

        return None

    land_counts = Counter()

    type_totals = {category: 0 for category in DECK_TYPE_CATEGORIES}

    type_totals["Other"] = 0

    for _ in range(trials):

        hand = random.sample(pool, hand_size)

        lands_in_hand = 0

        for card in hand:

            category = categorize_card_type(card.type_line)

            type_totals[category] += 1

            if category == "Land":

                lands_in_hand += 1

        land_counts[lands_in_hand] += 1

    land_distribution = [
        {
            "lands": n,
            "count": land_counts.get(n, 0),
            "pct": 100 * land_counts.get(n, 0) / trials,
        }
        for n in range(hand_size + 1)
    ]

    keepable_pct = 100 * sum(
        land_counts.get(n, 0)
        for n in KEEPABLE_LAND_RANGE
        if n <= hand_size
    ) / trials

    return {
        "trials": trials,
        "hand_size": hand_size,
        "avg_lands": sum(
            entry["lands"] * entry["count"] for entry in land_distribution
        ) / trials,
        "land_distribution": land_distribution,
        "keepable_pct": keepable_pct,
        "avg_by_type": {
            category: total / trials
            for category, total in type_totals.items()
            if total > 0
        },
    }


@app.get("/decks/{deck_id}/hand")
def deck_hand_simulator(
    request: Request,
    deck_id: int,
    db: Session = Depends(get_db),
):

    deck = (
        db.query(Deck)
        .filter(Deck.id == deck_id)
        .first()
    )

    if not deck:

        return RedirectResponse(
            url="/decks",
            status_code=303,
        )

    mainboard = [
        deck_card
        for deck_card in deck.cards
        if deck_card.section == "mainboard"
    ]

    pool = build_deck_card_pool(mainboard)

    return render_template(
        request,
        "hand_simulator.html",
        {
            "deck": deck,
            "pool_size": len(pool),
            "hand": draw_opening_hand(pool),
            "stats": simulate_hand_stats(pool),
        },
    )


@app.get("/decks/{deck_id}")
def deck_detail(
    request: Request,
    deck_id: int,
    search: str = "",
    set_code: str = "",
    card_type: str = "",
    color: str = "",
    rarity: str = "",
    finish: str = "",
    treatment: str = "",
    view: str = "list",
    deck_view: str = "basic",
    db: Session = Depends(get_db),
):

    if view not in ("list", "gallery"):

        view = "list"

    if deck_view not in ("basic", "detailed", "gallery"):

        deck_view = "basic"

    deck = (
        db.query(Deck)
        .filter(Deck.id == deck_id)
        .first()
    )

    if not deck:

        return RedirectResponse(
            url="/decks",
            status_code=303,
        )


    def _deck_card_sort_key(deck_card):

        card = deck_card.card

        return (
            card.set_code or "",
            _collector_sort_key(card.collector_number),
            card.name,
        )

    mainboard = sorted(
        (
            deck_card
            for deck_card in deck.cards
            if deck_card.section == "mainboard"
        ),
        key=_deck_card_sort_key,
    )

    sideboard = sorted(
        (
            deck_card
            for deck_card in deck.cards
            if deck_card.section == "sideboard"
        ),
        key=_deck_card_sort_key,
    )


    deck_card_ids = [
        deck_card.card_id
        for deck_card in deck.cards
    ]

    owned_by_card_id = (
        dict(
            db.query(
                Inventory.card_id,
                func.coalesce(
                    func.sum(Inventory.quantity),
                    0,
                ),
            )
            .filter(
                Inventory.card_id.in_(deck_card_ids)
            )
            .group_by(Inventory.card_id)
            .all()
        )
        if deck_card_ids
        else {}
    )


    inventory_rows = (
        build_collection_query(
            db,
            search=search,
            set_code=set_code,
            card_type=card_type,
            color=color,
            rarity=rarity,
            finish=finish,
            treatment=treatment,
        )
        .all()
    )

    browse_cards_by_id = {}

    for row in inventory_rows:

        entry = browse_cards_by_id.setdefault(
            row.card.id,
            {
                "card": row.card,
                "owned_qty": 0,
            },
        )

        entry["owned_qty"] += row.quantity

    browse_cards = sorted(
        browse_cards_by_id.values(),
        key=lambda entry: entry["card"].name,
    )


    filter_options = get_collection_filter_options(db)


    return render_template(
        request,
        "deck_detail.html",
        {
            "deck": deck,
            "mainboard": mainboard,
            "sideboard": sideboard,
            "mainboard_count": sum(
                deck_card.quantity for deck_card in mainboard
            ),
            "sideboard_count": sum(
                deck_card.quantity for deck_card in sideboard
            ),
            "composition": summarize_deck_composition(mainboard),
            "mainboard_cost": deck_section_cost(mainboard),
            "sideboard_cost": deck_section_cost(sideboard),
            "standard_legality":
                check_standard_legality(mainboard) if mainboard else None,
            "mana_base":
                check_mana_base(mainboard) if mainboard else None,
            "potential_tokens":
                find_potential_tokens(mainboard) if mainboard else [],
            "owned_by_card_id": owned_by_card_id,
            "browse_cards": browse_cards,
            "set_codes": filter_options["set_codes"],
            "rarities": filter_options["rarities"],
            "colors": COLOR_OPTIONS,
            "finishes": filter_options["finishes"],
            "treatments": filter_options["treatments"],
            "filters": {
                "search": search,
                "set_code": set_code,
                "card_type": card_type,
                "color": color,
                "rarity": rarity,
                "finish": finish,
                "treatment": treatment,
                "view": view,
                "deck_view": deck_view,
            },
        },
    )


# Shared with the "bracket" tool container via a docker-compose volume
# — decks exported here are how an external script (not part of this
# app) gets at deck lists to run its own tournaments against forge-sim.
DECK_EXPORT_DIR = Path(
    os.environ.get(
        "DECK_EXPORT_DIR",
        "/data/decks",
    )
)


# NOTE: registered before "/decks/{deck_id}" below — Starlette matches
# routes in registration order, and "export-all" would otherwise be
# swallowed by that route's {deck_id}: int path parameter (and 422,
# since it doesn't parse as an int).
@app.post("/decks/export-all")
def export_all_decks(
    db: Session = Depends(get_db),
):
    """Write every deck with a nonempty mainboard to DECK_EXPORT_DIR as
    a Forge .dck file, for an external script to read. Clears the
    directory first so it never accumulates decks that were since
    renamed, emptied, or deleted."""

    decks = (
        db.query(Deck)
        .all()
    )

    DECK_EXPORT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    for existing in DECK_EXPORT_DIR.glob("*.dck"):

        existing.unlink()

    exported = 0

    for deck in decks:

        mainboard_count = sum(
            deck_card.quantity
            for deck_card in deck.cards
            if deck_card.section == "mainboard"
        )

        if mainboard_count == 0:

            continue

        filename = (
            f"{slugify_deck_name(deck.name)}-{deck.id}.dck"
        )

        (DECK_EXPORT_DIR / filename).write_text(
            build_dck_text(deck)
        )

        exported += 1

    return RedirectResponse(
        url=f"/decks?exported={exported}",
        status_code=303,
    )


@app.post("/decks/{deck_id}")
def update_deck(
    deck_id: int,
    name: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):

    deck = (
        db.query(Deck)
        .filter(Deck.id == deck_id)
        .first()
    )

    if deck:

        deck.name = name.strip() or deck.name

        deck.notes = notes.strip() or None

        db.commit()


    return RedirectResponse(
        url=f"/decks/{deck_id}",
        status_code=303,
    )


@app.post("/decks/{deck_id}/delete")
def delete_deck(
    deck_id: int,
    db: Session = Depends(get_db),
):

    deck = (
        db.query(Deck)
        .filter(Deck.id == deck_id)
        .first()
    )

    if deck:

        db.delete(deck)

        db.commit()


    return RedirectResponse(
        url="/decks",
        status_code=303,
    )


def add_card_quantity_to_deck(
    db: Session,
    deck_id: int,
    card_id: int,
    section: str,
    quantity: int,
):
    """Increment (or create) the DeckCard row for card_id/section on
    deck_id, capped at the total owned quantity for that card. Shared
    by the manual add-to-deck endpoint and bulk-add's deck option.

    Returns a dict describing the resulting row —
    {"deck_card_id", "quantity", "deleted"} — so AJAX callers can
    patch the deck-detail page without a full reload.
    """

    if section not in DECK_SECTIONS:

        section = "mainboard"


    owned_qty = owned_quantity_for_card(db, card_id)

    deck_card = (
        db.query(DeckCard)
        .filter(
            DeckCard.deck_id == deck_id,
            DeckCard.card_id == card_id,
            DeckCard.section == section,
        )
        .first()
    )

    current_qty = deck_card.quantity if deck_card else 0

    new_qty = min(current_qty + quantity, owned_qty)

    if new_qty <= 0:

        existing_id = deck_card.id if deck_card else None

        if deck_card:

            db.delete(deck_card)

            db.commit()

        return {
            "deck_card_id": existing_id,
            "quantity": 0,
            "deleted": True,
        }

    if deck_card:

        deck_card.quantity = new_qty

    else:

        deck_card = DeckCard(
            deck_id=deck_id,
            card_id=card_id,
            section=section,
            quantity=new_qty,
        )

        db.add(deck_card)

    db.commit()

    return {
        "deck_card_id": deck_card.id,
        "quantity": deck_card.quantity,
        "deleted": False,
    }


def deck_section_counts(
    db: Session,
    deck_id: int,
):
    rows = (
        db.query(
            DeckCard.section,
            func.coalesce(func.sum(DeckCard.quantity), 0),
        )
        .filter(DeckCard.deck_id == deck_id)
        .group_by(DeckCard.section)
        .all()
    )

    counts = {section: 0 for section in DECK_SECTIONS}

    for section, total in rows:

        counts[section] = total

    return counts


def deck_section_costs(
    db: Session,
    deck_id: int,
):
    price_expression = func.coalesce(
        Card.price_usd,
        Card.price_usd_foil,
        Card.price_usd_etched,
        0,
    )

    rows = (
        db.query(
            DeckCard.section,
            func.coalesce(
                func.sum(DeckCard.quantity * price_expression),
                0,
            ),
        )
        .join(Card, Card.id == DeckCard.card_id)
        .filter(DeckCard.deck_id == deck_id)
        .group_by(DeckCard.section)
        .all()
    )

    costs = {section: Decimal("0.00") for section in DECK_SECTIONS}

    for section, total in rows:

        costs[section] = Decimal(total)

    return costs


def get_or_create_deck_by_name(
    db: Session,
    name: str,
):
    """Find a deck by exact (case-insensitive) name, or create it.
    Used by bulk-add's "add to deck" option so re-running a bulk add
    with the same deck name keeps adding to the same deck.
    """

    name = name.strip()

    if not name:

        return None

    deck = (
        db.query(Deck)
        .filter(func.lower(Deck.name) == name.lower())
        .first()
    )

    if deck:

        return deck

    deck = Deck(name=name)

    db.add(deck)

    db.commit()

    db.refresh(deck)

    return deck


def deck_mutation_response(
    request: Request,
    db: Session,
    deck_id: int,
    payload: dict,
):
    if is_ajax_request(request):

        counts = deck_section_counts(db, deck_id)

        payload["mainboard_count"] = counts["mainboard"]
        payload["sideboard_count"] = counts["sideboard"]

        costs = deck_section_costs(db, deck_id)

        payload["mainboard_cost"] = float(costs["mainboard"])
        payload["sideboard_cost"] = float(costs["sideboard"])

        return JSONResponse(payload)

    return RedirectResponse(
        url=f"/decks/{deck_id}",
        status_code=303,
    )


@app.post("/decks/{deck_id}/cards")
def add_card_to_deck(
    request: Request,
    deck_id: int,
    card_id: int = Form(...),
    section: str = Form("mainboard"),
    quantity: int = Form(1),
    db: Session = Depends(get_db),
):

    if section not in DECK_SECTIONS:

        section = "mainboard"

    result = add_card_quantity_to_deck(
        db,
        deck_id,
        card_id,
        section,
        quantity,
    )

    return deck_mutation_response(
        request,
        db,
        deck_id,
        {
            "card_id": card_id,
            "section": section,
            **result,
        },
    )


@app.post("/decks/{deck_id}/cards/{deck_card_id}/quantity")
def update_deck_card_quantity(
    request: Request,
    deck_id: int,
    deck_card_id: int,
    quantity: int = Form(...),
    db: Session = Depends(get_db),
):

    deck_card = (
        db.query(DeckCard)
        .filter(
            DeckCard.id == deck_card_id,
            DeckCard.deck_id == deck_id,
        )
        .first()
    )

    deleted = False
    final_qty = 0

    if deck_card:

        owned_qty = owned_quantity_for_card(
            db, deck_card.card_id
        )

        capped_qty = min(quantity, owned_qty)

        if capped_qty <= 0:

            deleted = True

            db.delete(deck_card)

        else:

            deck_card.quantity = capped_qty

            final_qty = capped_qty

        db.commit()


    return deck_mutation_response(
        request,
        db,
        deck_id,
        {
            "deck_card_id": deck_card_id,
            "quantity": final_qty,
            "deleted": deleted,
        },
    )


@app.post("/decks/{deck_id}/cards/{deck_card_id}/delete")
def delete_deck_card(
    request: Request,
    deck_id: int,
    deck_card_id: int,
    db: Session = Depends(get_db),
):

    deck_card = (
        db.query(DeckCard)
        .filter(
            DeckCard.id == deck_card_id,
            DeckCard.deck_id == deck_id,
        )
        .first()
    )

    if deck_card:

        db.delete(deck_card)

        db.commit()


    return deck_mutation_response(
        request,
        db,
        deck_id,
        {
            "deck_card_id": deck_card_id,
            "quantity": 0,
            "deleted": True,
        },
    )


@app.get("/decks/{deck_id}/export.txt")
def export_deck_txt(
    deck_id: int,
    db: Session = Depends(get_db),
):

    deck = (
        db.query(Deck)
        .filter(Deck.id == deck_id)
        .first()
    )

    if not deck:

        return RedirectResponse(
            url="/decks",
            status_code=303,
        )


    mainboard = sorted(
        (
            deck_card
            for deck_card in deck.cards
            if deck_card.section == "mainboard"
        ),
        key=lambda deck_card: deck_card.card.name,
    )

    sideboard = sorted(
        (
            deck_card
            for deck_card in deck.cards
            if deck_card.section == "sideboard"
        ),
        key=lambda deck_card: deck_card.card.name,
    )


    lines = ["Deck"]

    for deck_card in mainboard:

        lines.append(
            f"{deck_card.quantity} {deck_card.card.name} "
            f"({deck_card.card.set_code}) "
            f"{deck_card.card.collector_number}"
        )

    if sideboard:

        lines.append("")

        lines.append("Sideboard")

        for deck_card in sideboard:

            lines.append(
                f"{deck_card.quantity} {deck_card.card.name} "
                f"({deck_card.card.set_code}) "
                f"{deck_card.card.collector_number}"
            )

    content = "\n".join(lines) + "\n"


    return Response(
        content=content,
        media_type="text/plain",
        headers={
            "Content-Disposition":
                f'attachment; filename="{slugify_deck_name(deck.name)}_decklist.txt"',
        },
    )


FORGE_SIM_URL = "http://forge-sim:8000/simulate"

FORGE_SIM_TIMEOUT_SECONDS = 330


def build_dck_text(deck: Deck):
    """Build a Forge-format .dck (mainboard only — Forge's headless
    sim mode just needs two decks to play a match, no sideboard)."""

    mainboard = sorted(
        (
            deck_card
            for deck_card in deck.cards
            if deck_card.section == "mainboard"
        ),
        key=lambda deck_card: deck_card.card.name,
    )

    lines = [
        "[metadata]",
        f"Name={deck.name}",
        "[Main]",
    ]

    for deck_card in mainboard:

        lines.append(
            f"{deck_card.quantity} {deck_card.card.name}"
            f"|{deck_card.card.set_code}"
        )

    return "\n".join(lines) + "\n"


@app.get("/simulate")
def simulate_form(
    request: Request,
    db: Session = Depends(get_db),
):

    decks = (
        db.query(Deck)
        .order_by(Deck.name)
        .all()
    )

    return render_template(
        request,
        "simulate.html",
        {
            "decks": decks,
            "result": None,
        },
    )


@app.post("/simulate/run")
def simulate_run(
    request: Request,
    deck_a_id: int = Form(...),
    deck_b_id: int = Form(...),
    games: int = Form(20),
    db: Session = Depends(get_db),
):

    decks = (
        db.query(Deck)
        .order_by(Deck.name)
        .all()
    )

    deck_a = (
        db.query(Deck)
        .filter(Deck.id == deck_a_id)
        .first()
    )

    deck_b = (
        db.query(Deck)
        .filter(Deck.id == deck_b_id)
        .first()
    )

    if not deck_a or not deck_b:

        return render_template(
            request,
            "simulate.html",
            {
                "decks": decks,
                "result": {
                    "error": "Pick two decks to simulate.",
                },
            },
        )


    payload = {
        "deck_a_name": deck_a.name,
        "deck_a_dck": build_dck_text(deck_a),
        "deck_b_name": deck_b.name,
        "deck_b_dck": build_dck_text(deck_b),
        "games": games,
    }

    try:

        response = requests.post(
            FORGE_SIM_URL,
            json=payload,
            timeout=FORGE_SIM_TIMEOUT_SECONDS,
        )

        response.raise_for_status()

        result = response.json()

    except requests.exceptions.Timeout:

        result = {
            "error":
                "Simulation is taking longer than expected — it may "
                "still be running (possibly queued behind another "
                "simulation). Try again in a bit.",
        }

    except Exception:

        result = {
            "error":
                "Simulation service unavailable — "
                "is the forge-sim container running?",
        }


    result["deck_a_name"] = deck_a.name

    result["deck_b_name"] = deck_b.name


    return render_template(
        request,
        "simulate.html",
        {
            "decks": decks,
            "result": result,
            "selected_deck_a_id": deck_a_id,
            "selected_deck_b_id": deck_b_id,
            "selected_games": games,
        },
    )


def is_ajax_request(
    request: Request,
):
    """Our own collection-page JS marks its fetch() calls with this
    header so these endpoints can respond with JSON instead of a
    redirect, letting the page update in place without losing filters,
    sort, or scroll position."""

    return bool(
        request.headers.get("x-requested-with")
    )


def quantity_update_response(
    request: Request,
    inventory: Inventory | None,
    deleted: bool,
):
    if is_ajax_request(request):

        if not inventory or deleted:

            return JSONResponse({
                "quantity": 0,
                "deleted": True,
                "entry_value": 0.0,
            })

        entry_value = (
            inventory_price(inventory)
            * inventory.quantity
        )

        return JSONResponse({
            "quantity": inventory.quantity,
            "deleted": False,
            "entry_value": float(entry_value),
        })

    return RedirectResponse(
        url="/collection",
        status_code=303,
    )


@app.post(
    "/inventory/{inventory_id}/quantity"
)
def update_quantity(

    request: Request,

    inventory_id: int,

    quantity: int = Form(...),

    db: Session = Depends(get_db),

):

    inventory = (
        db.query(Inventory)
        .filter(
            Inventory.id ==
                inventory_id
        )
        .first()
    )


    if not inventory:

        return quantity_update_response(request, None, deleted=True)


    deleted = quantity <= 0

    if deleted:

        db.delete(
            inventory
        )

    else:

        inventory.quantity = (
            quantity
        )


    db.commit()


    return quantity_update_response(request, inventory, deleted)


@app.post(
    "/inventory/{inventory_id}/adjust"
)
def adjust_quantity(

    request: Request,

    inventory_id: int,

    amount: int = Form(...),

    db: Session = Depends(get_db),

):

    inventory = (
        db.query(Inventory)
        .filter(
            Inventory.id ==
                inventory_id
        )
        .first()
    )


    if not inventory:

        return quantity_update_response(request, None, deleted=True)


    inventory.quantity += amount


    deleted = inventory.quantity <= 0

    if deleted:

        db.delete(
            inventory
        )


    db.commit()


    return quantity_update_response(request, inventory, deleted)


@app.post(
    "/inventory/{inventory_id}/details"
)
def update_details(

    inventory_id: int,

    finish: str = Form(...),

    treatment: str = Form(...),

    db: Session = Depends(get_db),

):

    inventory = (
        db.query(Inventory)
        .filter(
            Inventory.id ==
                inventory_id
        )
        .first()
    )


    if not inventory:

        return RedirectResponse(
            url="/collection",
            status_code=303,
        )


    finish = (
        finish
        .strip()
        .lower()
        or "normal"
    )


    treatment = (
        treatment
        .strip()
        .lower()
        or "regular"
    )


    # Collision check must stay within the same back-face pairing (or
    # lack thereof) as the row being edited — otherwise changing
    # finish/treatment on an unlinked row could silently merge it into
    # an unrelated, already-linked pairing that just happens to share
    # a finish/treatment.
    back_face_match = (
        Inventory.back_card_id.is_(None)
        if inventory.back_card_id is None
        else Inventory.back_card_id == inventory.back_card_id
    )

    existing = (
        db.query(Inventory)
        .filter(
            Inventory.card_id ==
                inventory.card_id,

            Inventory.finish ==
                finish,

            Inventory.treatment ==
                treatment,

            back_face_match,

            Inventory.id !=
                inventory.id,
        )
        .first()
    )


    if existing:

        existing.quantity += (
            inventory.quantity
        )

        db.delete(
            inventory
        )

    else:

        inventory.finish = finish

        inventory.treatment = treatment


    db.commit()


    return RedirectResponse(
        url="/collection",
        status_code=303,
    )


@app.post(
    "/inventory/{inventory_id}/back-face"
)
def update_back_face(

    inventory_id: int,

    back_set_code: str = Form(""),

    back_collector_number: str = Form(""),

    db: Session = Depends(get_db),

):
    """Attach, change, or clear the linked back-face card for an
    existing inventory row's card — lets a token added before this
    feature existed be backfilled without deleting and re-adding it.
    """

    inventory = (
        db.query(Inventory)
        .filter(
            Inventory.id ==
                inventory_id
        )
        .first()
    )

    if not inventory:

        return RedirectResponse(
            url="/collection",
            status_code=303,
        )


    back_collector_number = back_collector_number.strip()

    if not back_collector_number:

        unlink_back_face(db, inventory.id)

        return RedirectResponse(
            url="/collection",
            status_code=303,
        )


    back_card = get_or_create_card(
        db,
        back_set_code.strip() or inventory.card.set_code,
        back_collector_number,
    )

    if back_card:

        link_back_face(db, inventory.id, back_card.id)


    return RedirectResponse(
        url="/collection",
        status_code=303,
    )


@app.post(
    "/inventory/{inventory_id}/delete"
)
def delete_inventory(

    inventory_id: int,

    db: Session = Depends(get_db),

):

    inventory = (
        db.query(Inventory)
        .filter(
            Inventory.id ==
                inventory_id
        )
        .first()
    )


    if inventory:

        db.delete(
            inventory
        )

        db.commit()


    return RedirectResponse(
        url="/collection",
        status_code=303,
    )


@app.post(
    "/inventory/{inventory_id}/refresh-price"
)
def refresh_single_price(

    inventory_id: int,

    db: Session = Depends(get_db),

):

    inventory = (
        db.query(Inventory)
        .filter(
            Inventory.id ==
                inventory_id
        )
        .first()
    )


    if inventory:

        try:

            refresh_card_data(
                inventory.card
            )

            db.commit()

        except Exception:

            db.rollback()


    return RedirectResponse(
        url="/collection",
        status_code=303,
    )


def refresh_all_card_prices(db: Session):

    cards = (
        db.query(Card)
        .all()
    )

    for card in cards:

        try:

            refresh_card_data(
                card
            )

            db.commit()

        except Exception:

            db.rollback()


def compute_collection_totals(db: Session):
    """Total quantity and total USD value across all inventory —
    the same math sets_summary uses per-set, just unrolled to a
    single grand total for value-history snapshots."""

    inventory_entries = (
        db.query(Inventory)
        .all()
    )

    total_cards = sum(
        entry.quantity for entry in inventory_entries
    )

    total_value = sum(
        (
            inventory_price(entry) * entry.quantity
            for entry in inventory_entries
        ),
        Decimal("0.00"),
    )

    return total_cards, total_value


def record_collection_value_snapshot(db: Session):

    total_cards, total_value = compute_collection_totals(db)

    db.add(
        CollectionValueSnapshot(
            total_cards=total_cards,
            total_value=total_value,
        )
    )

    db.commit()


def refresh_prices_and_snapshot(db: Session):

    refresh_all_card_prices(db)

    record_collection_value_snapshot(db)


@app.post("/refresh-prices")
def refresh_all_prices(

    db: Session = Depends(get_db),

):

    refresh_prices_and_snapshot(db)

    return RedirectResponse(
        url="/collection",
        status_code=303,
    )


VALUE_CHART_WIDTH = 700

VALUE_CHART_HEIGHT = 220

VALUE_CHART_PADDING = 30


def build_value_history_chart(snapshots):
    """Turn a time-ordered list of CollectionValueSnapshot rows into
    SVG polyline points, positioned by actual elapsed time (not just
    evenly spaced by index)."""

    if len(snapshots) < 2:

        return None

    values = [float(snapshot.total_value) for snapshot in snapshots]

    min_value, max_value = min(values), max(values)

    if min_value == max_value:

        min_value -= 1
        max_value += 1

    times = [snapshot.captured_at for snapshot in snapshots]

    span_seconds = (
        (times[-1] - times[0]).total_seconds()
        or 1
    )

    plot_width = VALUE_CHART_WIDTH - 2 * VALUE_CHART_PADDING

    plot_height = VALUE_CHART_HEIGHT - 2 * VALUE_CHART_PADDING

    points = []

    for snapshot, value, captured_at in zip(snapshots, values, times):

        x = (
            VALUE_CHART_PADDING
            + (captured_at - times[0]).total_seconds() / span_seconds * plot_width
        )

        y = (
            VALUE_CHART_PADDING
            + plot_height
            - (value - min_value) / (max_value - min_value) * plot_height
        )

        points.append({
            "x": round(x, 1),
            "y": round(y, 1),
            "value": value,
            "date": captured_at.strftime("%Y-%m-%d"),
        })

    return {
        "width": VALUE_CHART_WIDTH,
        "height": VALUE_CHART_HEIGHT,
        "points": points,
        "polyline":
            " ".join(f"{point['x']},{point['y']}" for point in points),
    }


@app.get("/sets")
def sets_summary(

    request: Request,

    db: Session = Depends(get_db),

):

    cards = (
        db.query(Card)
        .all()
    )


    summaries = {}


    for card in cards:

        key = card.set_code


        if key not in summaries:

            summaries[key] = {

                "set_code":
                    card.set_code,

                "set_name":
                    card.set_name
                    or card.set_code,

                "total_cards":
                    0,

                "unique_cards":
                    0,

                "total_value":
                    Decimal("0.00"),

            }


        inventory_entries = (
            db.query(Inventory)
            .filter(
                Inventory.card_id ==
                    card.id
            )
            .all()
        )


        card_has_inventory = False


        for inventory in (
            inventory_entries
        ):

            card_has_inventory = True


            summaries[key][
                "total_cards"
            ] += inventory.quantity


            price = inventory_price(
                inventory
            )


            summaries[key][
                "total_value"
            ] += (
                price
                * inventory.quantity
            )


        if card_has_inventory:

            summaries[key][
                "unique_cards"
            ] += 1


    set_rows = sorted(

        summaries.values(),

        key=lambda row:
            row["set_name"].lower(),

    )


    grand_total_cards = sum(

        row["total_cards"]

        for row in set_rows

    )


    grand_total_value = sum(

        row["total_value"]

        for row in set_rows

    )


    value_snapshots = (
        db.query(CollectionValueSnapshot)
        .order_by(CollectionValueSnapshot.captured_at)
        .all()
    )

    return render_template(

        request,

        "sets.html",

        {

            "set_rows":
                set_rows,

            "grand_total_cards":
                grand_total_cards,

            "grand_total_value":
                grand_total_value,

            "value_history":
                build_value_history_chart(value_snapshots),

        },
    )
