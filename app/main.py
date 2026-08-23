import csv
import re

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
    RedirectResponse,
    Response,
)

from fastapi.templating import Jinja2Templates

from sqlalchemy import (
    case,
    func,
)

from sqlalchemy.orm import Session

from .database import get_db

from .models import (
    Card,
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


@asynccontextmanager
async def lifespan(app: FastAPI):

    # Database schema migrations are now handled
    # by Alembic before FastAPI starts.

    yield


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


    inventory = (
        db.query(Inventory)
        .filter(
            Inventory.card_id ==
                card.id,

            Inventory.finish ==
                finish,

            Inventory.treatment ==
                treatment,
        )
        .first()
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
):

    return render_template(
        request,
        "bulk_add.html",
        {
            "results": None,
        },
    )


@app.post("/bulk-add")
def bulk_add(
    request: Request,

    set_code: str = Form(...),

    finish: str = Form(...),

    treatment: str = Form(...),

    collector_number: List[str] = Form(default=[]),

    quantity: List[str] = Form(default=[]),

    db: Session = Depends(get_db),
):

    results = []

    for number, qty_raw in zip(
        collector_number,
        quantity,
    ):

        number = number.strip()

        if not number:

            continue


        try:

            qty = int(
                (qty_raw or "").strip()
                or "1"
            )

        except ValueError:

            results.append({
                "collector_number": number,
                "success": False,
                "message": "Quantity must be a number",
            })

            continue


        success, message = add_card_to_collection(
            db,
            set_code,
            number,
            finish,
            treatment,
            qty,
        )

        results.append({
            "collector_number": number,
            "quantity": qty,
            "success": success,
            "message": message,
        })


    return render_template(
        request,
        "bulk_add.html",
        {
            "results": results,
            "last_set_code": set_code.upper().strip(),
            "last_finish": finish,
            "last_treatment": treatment,
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
    sort: str = "name",
    direction: str = "asc",
):
    """Build the filtered/sorted Inventory query shared by the
    collection page and the CSV export, so both stay in sync."""

    query = (
        db.query(Inventory)
        .join(Card)
    )


    if search:

        query = query.filter(
            Card.name.ilike(
                f"%{search.strip()}%"
            )
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
            order_column.desc()
        )

    else:

        query = query.order_by(
            order_column.asc()
        )


    if sort == "set":

        query = query.order_by(
            Card.set_code.asc(),
            Card.collector_number.asc(),
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
            Inventory.card_id ==
                card_id
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
            },
        },
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


@app.post("/decks/{deck_id}/cards")
def add_card_to_deck(
    deck_id: int,
    card_id: int = Form(...),
    section: str = Form("mainboard"),
    quantity: int = Form(1),
    db: Session = Depends(get_db),
):

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

        if deck_card:

            db.delete(deck_card)

            db.commit()

        return RedirectResponse(
            url=f"/decks/{deck_id}",
            status_code=303,
        )

    if deck_card:

        deck_card.quantity = new_qty

    else:

        db.add(
            DeckCard(
                deck_id=deck_id,
                card_id=card_id,
                section=section,
                quantity=new_qty,
            )
        )

    db.commit()


    return RedirectResponse(
        url=f"/decks/{deck_id}",
        status_code=303,
    )


@app.post("/decks/{deck_id}/cards/{deck_card_id}/quantity")
def update_deck_card_quantity(
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

    if deck_card:

        owned_qty = owned_quantity_for_card(
            db, deck_card.card_id
        )

        capped_qty = min(quantity, owned_qty)

        if capped_qty <= 0:

            db.delete(deck_card)

        else:

            deck_card.quantity = capped_qty

        db.commit()


    return RedirectResponse(
        url=f"/decks/{deck_id}",
        status_code=303,
    )


@app.post("/decks/{deck_id}/cards/{deck_card_id}/delete")
def delete_deck_card(
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


    return RedirectResponse(
        url=f"/decks/{deck_id}",
        status_code=303,
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


@app.post(
    "/inventory/{inventory_id}/quantity"
)
def update_quantity(

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

        return RedirectResponse(
            url="/collection",
            status_code=303,
        )


    if quantity <= 0:

        db.delete(
            inventory
        )

    else:

        inventory.quantity = (
            quantity
        )


    db.commit()


    return RedirectResponse(
        url="/collection",
        status_code=303,
    )


@app.post(
    "/inventory/{inventory_id}/adjust"
)
def adjust_quantity(

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

        return RedirectResponse(
            url="/collection",
            status_code=303,
        )


    inventory.quantity += amount


    if inventory.quantity <= 0:

        db.delete(
            inventory
        )


    db.commit()


    return RedirectResponse(
        url="/collection",
        status_code=303,
    )


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


    existing = (
        db.query(Inventory)
        .filter(
            Inventory.card_id ==
                inventory.card_id,

            Inventory.finish ==
                finish,

            Inventory.treatment ==
                treatment,

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


@app.post("/refresh-prices")
def refresh_all_prices(

    db: Session = Depends(get_db),

):

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


    return RedirectResponse(
        url="/collection",
        status_code=303,
    )


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

        },
    )
