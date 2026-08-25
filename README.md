# mtg-tracker

A self-hosted Magic: The Gathering collection tracker and deck builder.
Card data and prices come from [Scryfall](https://scryfall.com); a
sidecar service can play out simulated matches — one-off or full
bracket tournaments — between decks using
[Forge](https://github.com/Card-Forge/forge)'s headless AI.

## Features

**Collection**
- Add cards by set code + collector number (looked up and cached from
  Scryfall on first add); track quantity, finish (normal/foil/etched),
  and treatment separately per card.
- Bulk-add: paste a list of collector numbers/quantities for one set at
  once, optionally routing them straight into a deck (existing or new)
  at the same time.
- Filter/sort/search the collection by name, set, type, color, rarity,
  finish, or treatment; card/list/gallery views.
- Per-set summary page (`/sets`) with owned counts, total value, and a
  collection-value-over-time chart, filled in automatically by a weekly
  price refresh (or any manual "Refresh Prices" click).
- Export the (optionally filtered) collection as CSV formatted for
  either [Moxfield's](https://moxfield.com) or
  [ManaBox's](https://manabox.app) importers.
- "Refresh prices" re-pulls current Scryfall pricing (and legality /
  mana-producing data) for one card or the whole collection.

**Decks**
- Create decks manually, or import an Arena/MTGA-format decklist (the
  format Moxfield, Archidekt, and ManaBox all export) — parses quantity,
  name, set, and collector number per line, with mainboard/sideboard
  section headers.
- Deck detail page: browse your owned collection alongside the deck and
  add/adjust card quantities in place (AJAX, no page reload), capped at
  what you actually own. Basic/detailed/gallery view modes.
- Deck composition breakdown: card-type mix, colored-mana-symbol (pip)
  weighting, and mana curve, plus mainboard/sideboard cost totals.
- Soft, non-blocking warnings for Standard legality (banned/rotated
  cards, over-the-limit copy counts) and mana base gaps (a color a
  spell needs that nothing in the deck produces).
- Opening Hand Simulator (`/decks/{id}/hand`) — draws a sample 7-card
  hand and reports land-count and card-type odds over 10,000 simulated
  draws.
- Export a deck back out as an Arena-format `.txt` decklist.

**Simulate**
- Pick two decks and run N AI-vs-AI games between them via the
  `forge-sim` sidecar. Games are split across several Forge processes
  in parallel, so larger game counts don't take proportionally longer;
  reports the win/loss split and average game duration.

**Bracket Tournaments**
- `./run-bracket.sh` exports every deck with a nonempty mainboard and
  runs a single-elimination bracket across all of them, with each
  matchup played best-of-11 (configurable) so one unlucky draw doesn't
  eliminate an otherwise-strong deck. Prints round-by-round results and
  final standings. See `bracket/bracket.py` for options (`--games`,
  `--seed`).

## Stack

- **App:** FastAPI + Jinja2 templates (server-rendered, with small AJAX
  enhancements for in-place deck/collection editing) — `app/`
- **DB:** Postgres, via SQLAlchemy models (`app/models.py`) and Alembic
  migrations (`alembic/versions/`)
- **External data:** Scryfall REST API (`app/scryfall.py`)
- **Simulator sidecar:** `forge-sim/` — a FastAPI wrapper
  (`forge-sim/server.py`) around a Forge JAR running headless under
  `xvfb-run`. A single `/simulate` request fans games out across
  several parallel Forge processes (own process group each, so a
  timed-out worker gets fully killed rather than leaking a JVM),
  guarded by a lock so two simulate requests can't corrupt each other's
  deck files. The main app calls it over the internal Compose network
  (`http://forge-sim:8000/simulate`).
- **Bracket tool:** `bracket/` — a standalone script, *not* part of the
  web app, that reads decks mtg-tracker exports to a shared Docker
  volume and calls `forge-sim` directly to run a tournament. It's a
  Compose profile (`bracket`), so it doesn't start with the normal
  stack — run it with `./run-bracket.sh` or
  `docker compose run --rm bracket`.

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

### Running a bracket tournament

With the stack up:

```
./run-bracket.sh                       # best-of-11, random seed
./run-bracket.sh --games 21 --seed 42  # override game count / seeding
```

This exports your current decks and plays a full single-elimination
bracket, printing each round's matchup scores and a final standings
list to the terminal.

## Notes

- Postgres data lives in `./postgres_data` (bind mount, gitignored) —
  container-owned, not readable by a non-root host user.
- No auth on the web UI — it's expected to run on a trusted local network
  only.
