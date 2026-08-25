"""Single-elimination bracket runner for decks exported by mtg-tracker.

Reads every .dck file from DECKS_DIR (mtg-tracker's "Export All to
Bracket Folder" button writes these), seeds a random bracket, and
plays each matchup through forge-sim's /simulate endpoint — the same
service the main app's Simulate page uses, already parallelized across
several Forge processes per match. Prints the bracket round by round
and a final standings list.

Each matchup is best-of-N (11 games by default) rather than a single
game, so one unlucky draw or mulligan doesn't knock a deck out of the
bracket — the deck with more wins across the N games advances.

Usage (from the repo root, with the stack already up):
    docker compose run --rm bracket
    docker compose run --rm bracket --games 31 --seed 42
"""

import argparse
import os
import random
import re
import sys
from pathlib import Path

import requests


DECKS_DIR = Path(os.environ.get("DECKS_DIR", "/data/decks"))

FORGE_SIM_URL = os.environ.get(
    "FORGE_SIM_URL",
    "http://forge-sim:8000/simulate",
)

# Best-of-11 by default — override per run with --games. Odd, so a
# matchup can never end in a tie.
DEFAULT_GAMES_PER_MATCH = 11

# Generous — forge-sim itself budgets up to ~300s per match internally
# (parallel workers), plus queueing time behind any other simulation
# already in flight.
REQUEST_TIMEOUT_SECONDS = 400

NAME_RE = re.compile(r"^Name=(.+)$", re.MULTILINE)


def load_decks():

    decks = []

    for path in sorted(DECKS_DIR.glob("*.dck")):

        text = path.read_text()

        match = NAME_RE.search(text)

        name = match.group(1).strip() if match else path.stem

        decks.append({
            "name": name,
            "dck": text,
        })

    return decks


def play_match(deck_a, deck_b, games):

    payload = {
        "deck_a_name": deck_a["name"],
        "deck_a_dck": deck_a["dck"],
        "deck_b_name": deck_b["name"],
        "deck_b_dck": deck_b["dck"],
        "games": games,
    }

    response = requests.post(
        FORGE_SIM_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )

    response.raise_for_status()

    result = response.json()

    if result.get("error") or not result.get("games_played"):

        raise RuntimeError(
            f"{deck_a['name']} vs {deck_b['name']} failed: "
            f"{result.get('error', 'no games completed')}"
        )

    return result


def run_round(decks, round_num, games, rng):

    print(f"\n=== Round {round_num} ({len(decks)} decks) ===", flush=True)

    winners = []
    losers = []

    pairs = list(zip(decks[0::2], decks[1::2]))

    bye = decks[-1] if len(decks) % 2 else None

    for deck_a, deck_b in pairs:

        print(
            f"  {deck_a['name']} vs {deck_b['name']} "
            f"({games} games)... ",
            end="",
            flush=True,
        )

        result = play_match(deck_a, deck_b, games)

        if result["deck_a_wins"] > result["deck_b_wins"]:

            winner, loser = deck_a, deck_b

        elif result["deck_b_wins"] > result["deck_a_wins"]:

            winner, loser = deck_b, deck_a

        else:

            winner, loser = rng.sample([deck_a, deck_b], 2)

            print("(tied — coin flip) ", end="")

        print(
            f"{winner['name']} wins "
            f"({result['deck_a_wins']}-{result['deck_b_wins']})"
        )

        if result.get("note"):

            print(f"    note: {result['note']}")

        winners.append(winner)
        losers.append(loser)

    if bye:

        print(f"  {bye['name']} gets a bye")

        winners.append(bye)

    return winners, losers


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Run a single-elimination bracket between all decks "
            "exported by mtg-tracker."
        )
    )

    parser.add_argument(
        "--games",
        type=int,
        default=DEFAULT_GAMES_PER_MATCH,
        help="Games per matchup (default: %(default)s)",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Random seed for bracket seeding (default: random)",
    )

    args = parser.parse_args()

    decks = load_decks()

    if len(decks) < 2:

        print(
            f"Need at least 2 exported decks in {DECKS_DIR} — found "
            f"{len(decks)}. Use \"Export All to Bracket Folder\" on "
            f"mtg-tracker's Decks page first."
        )

        sys.exit(1)

    rng = random.Random(args.seed)

    rng.shuffle(decks)

    all_decks = list(decks)

    print(f"Bracket with {len(decks)} decks, {args.games} games per matchup:")

    for deck in decks:

        print(f"  - {deck['name']}")

    eliminated_at = {}

    round_num = 1

    try:

        while len(decks) > 1:

            decks, round_losers = run_round(decks, round_num, args.games, rng)

            for loser in round_losers:

                eliminated_at[loser["name"]] = round_num

            round_num += 1

    except (requests.exceptions.RequestException, RuntimeError) as error:

        print(f"\nBracket aborted: {error}")

        sys.exit(1)

    champion = decks[0]

    print(f"\n🏆 Champion: {champion['name']}\n")

    print("Final standings:")

    def sort_key(deck):

        if deck["name"] == champion["name"]:

            return (float("inf"), deck["name"])

        return (eliminated_at.get(deck["name"], 0), deck["name"])

    standings = sorted(all_decks, key=sort_key, reverse=True)

    for position, deck in enumerate(standings, start=1):

        if deck["name"] == champion["name"]:

            note = "Champion"

        else:

            note = f"eliminated round {eliminated_at[deck['name']]}"

        print(f"  {position}. {deck['name']} — {note}")


if __name__ == "__main__":

    main()
