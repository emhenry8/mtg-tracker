import csv

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


    if not card:

        try:

            scryfall_data = (
                get_card_by_set_and_number(
                    set_code,
                    collector_number,
                )
            )

        except Exception:

            return False, "Could not contact Scryfall"


        if not scryfall_data:

            return (
                False,
                f"Card not found: {set_code} #{collector_number}",
            )


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
                set_codes,

            "rarities":
                rarities,

            "colors":
                COLOR_OPTIONS,

            "finishes":
                finishes,

            "treatments":
                treatments,

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
