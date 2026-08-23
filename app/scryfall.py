import time
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests


SCRYFALL_BASE_URL = (
    "https://api.scryfall.com"
)


HEADERS = {

    "User-Agent":
        "MTGCollectionTracker/1.0",

    "Accept":
        "application/json",

}


def get_card_by_set_and_number(

    set_code: str,

    collector_number: str,

):

    url = (

        f"{SCRYFALL_BASE_URL}/cards/"

        f"{set_code.lower()}/"

        f"{collector_number}"

    )


    response = requests.get(

        url,

        headers=HEADERS,

        timeout=20,

    )


    if response.status_code == 404:

        return None


    response.raise_for_status()


    return response.json()


def safe_decimal(value):

    if value is None:

        return None


    try:

        return Decimal(
            str(value)
        )

    except (
        InvalidOperation,
        ValueError,
        TypeError,
    ):

        return None


def extract_card_data(
    data: dict,
):

    prices = (
        data.get("prices")
        or {}
    )


    image_uris = (
        data.get("image_uris")
        or (
            data.get(
                "card_faces",
                [{}],
            )[0]
            .get("image_uris")
        )
        or {}
    )


    return {

        "scryfall_id":
            data.get("id"),

        "name":
            data.get("name"),

        "set_code":
            data.get(
                "set",
                "",
            ).upper(),

        "set_name":
            data.get(
                "set_name"
            ),

        "collector_number":
            data.get(
                "collector_number"
            ),

        "type_line":
            data.get(
                "type_line"
            ),

        "rarity":
            data.get(
                "rarity"
            ),

        "mana_cost":
            data.get(
                "mana_cost"
            ),

        "oracle_text":
            data.get(
                "oracle_text"
            ),

        "colors":
            ",".join(
                data.get(
                    "colors",
                    [],
                )
            ),

        "image_url":
            image_uris.get("normal")
            or image_uris.get("large"),

        "cmc":
            data.get(
                "cmc"
            ),

        "price_usd":
            safe_decimal(
                prices.get("usd")
            ),

        "price_usd_foil":
            safe_decimal(
                prices.get(
                    "usd_foil"
                )
            ),

        "price_usd_etched":
            safe_decimal(
                prices.get(
                    "usd_etched"
                )
            ),

        "price_updated_at":
            datetime.utcnow(),

    }


def refresh_card_data(card):

    data = get_card_by_set_and_number(

        card.set_code,

        card.collector_number,

    )


    if not data:

        return False


    card_data = (
        extract_card_data(
            data
        )
    )


    for key, value in (
        card_data.items()
    ):

        setattr(
            card,
            key,
            value,
        )


    # Avoid making requests too quickly.

    time.sleep(0.12)


    return True


def get_variant_price(
    card,
    finish: str,
):

    finish = (
        finish
        or ""
    ).lower().strip()


    if finish == "foil":

        return (

            card.price_usd_foil

            or card.price_usd

        )


    if finish == "etched":

        return (

            card.price_usd_etched

            or card.price_usd_foil

            or card.price_usd

        )


    return card.price_usd
