# mtg-tracker

A self-hosted Magic: The Gathering collection tracker and deck builder.
Card data and prices come from [Scryfall](https://scryfall.com); a
sidecar service can play out simulated matches between two decks using
[Forge](https://github.com/Card-Forge/forge)'s headless AI.

## Features

**Collection**
- Add cards by set code + collector number (looked up and cached from
  Scryfall on first add); track quantity, finish (normal/foil/etched),
  and treatment separately per card.
- Bulk-add: paste a list of collector numbers/quantities for one set at
  once, optionally routing them straight into a deck.
- Filter/sort/search the collection by name, set, type, color, rarity,
  finish, or treatment; card/list/gallery views.
- Per-set summary page (`/sets`) with owned counts and total value.
- Export the (optionally filtered) collection as CSV formatted for
  either [Moxfield's](https://moxfield.com) or
  [ManaBox's](https://manabox.app) importers.
- "Refresh prices" re-pulls current Scryfall pricing for one card or the
  whole collection.

**Decks**
- Create decks manually, or import an Arena/MTGA-format decklist (the
  format Moxfield, Archidekt, and ManaBox all export) — parses quantity,
  name, set, and collector number per line, with mainboard/sideboard
  section headers.
- Deck detail page: browse your owned collection alongside the deck and
  add/adjust card quantities in place (AJAX, no page reload), capped at
  what you actually own.
- Export a deck back out as an Arena-format `.txt` decklist.

**Simulate**
- Pick two decks and run N AI-vs-AI games between them via the
  `forge-sim` sidecar, which shells out to Forge's headless sim mode and
  reports the win/loss split and average game duration.

## Stack

- **App:** FastAPI + Jinja2 templates (server-rendered, with small AJAX
  enhancements for in-place deck editing) — `app/`
- **DB:** Postgres, via SQLAlchemy models (`app/models.py`) and Alembic
  migrations (`alembic/versions/`)
- **External data:** Scryfall REST API (`app/scryfall.py`)
- **Simulator sidecar:** `forge-sim/` — a tiny FastAPI wrapper
  (`forge-sim/server.py`) around a Forge JAR running headless under
  `xvfb-run`; the main app calls it over the internal Compose network
  (`http://forge-sim:8000/simulate`)

## Running it

```
cp .env.example .env   # if present — otherwise create .env per below
docker compose up -d --build
```

Required `.env` values (gitignored, not committed):

```
POSTGRES_DB=mtgtracker
POSTGRES_USER=mtg
POSTGRES_PASSWORD=<your password>
```

The app runs migrations automatically on startup (`alembic upgrade head`
before `uvicorn` starts — see `Dockerfile`) and is served on
`localhost:8010`.

`bkp.docker-compose.yml` is a pre-`forge-sim` snapshot of the compose
file, kept for reference — not used by anything.

## Notes

- Postgres data lives in `./postgres_data` (bind mount, gitignored) —
  container-owned, not readable by a non-root host user.
- No auth on the web UI — it's expected to run on a trusted local network
  only.
