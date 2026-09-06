"""Bracket runner for decks exported by mtg-tracker.

Reads every .dck file from DECKS_DIR (mtg-tracker's "Export All to
Bracket Folder" button writes these), seeds a random bracket, and
plays each matchup through forge-sim's /simulate endpoint — the same
service the main app's Simulate page uses, already parallelized across
several Forge processes per match. Prints the bracket round by round
and a final standings list, and saves that same output to a timestamped
summary file under RESULTS_DIR.

Each matchup is best-of-N (11 games by default) rather than a single
game, so one unlucky draw or mulligan doesn't knock a deck out of the
bracket — the deck with more wins across the N games advances.

Two bracket formats (--format):
  single      Standard single-elimination — one loss and you're out.
  double-elim Everyone can lose once before being eliminated (out on
              a 2nd loss), so one unlucky round against the eventual
              best deck doesn't end your run the way it does in
              single-elim. This isn't a formally-seeded winners/losers
              bracket with strict re-entry rules (that has real edge
              cases for non-power-of-2 fields) — it's a shared pool
              where everyone keeps playing until they've lost twice,
              pairing away from repeat matchups where possible. Same
              practical benefit, much simpler for an arbitrary deck
              count.

Usage (from the repo root, with the stack already up):
    docker compose run --rm bracket
    docker compose run --rm bracket --games 31 --seed 42
    docker compose run --rm bracket --format double-elim
"""

import argparse
import os
import random
import re
import sys
from datetime import datetime
from pathlib import Path

import requests


DECKS_DIR = Path(os.environ.get("DECKS_DIR", "/data/decks"))

RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "/data/results"))

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


class RunLog:
    """Mirrors every print() to stdout (so a live run still shows
    progress) while also buffering it line-by-line so the whole run
    can be flushed to a summary file — including a run that gets cut
    short by an error, since save() is called from the except block
    too rather than only on a clean finish.
    """

    def __init__(self):

        self.lines = []

        self._partial_line = ""

    def write(self, text="", end="\n"):

        print(text, end=end, flush=True)

        self._partial_line += text + end

        while "\n" in self._partial_line:

            line, self._partial_line = self._partial_line.split("\n", 1)

            self.lines.append(line)

    def save(self, path):

        if self._partial_line:

            self.lines.append(self._partial_line)

            self._partial_line = ""

        path.parent.mkdir(parents=True, exist_ok=True)

        path.write_text("\n".join(self.lines) + "\n")


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


def play_and_log_match(deck_a, deck_b, games, rng, log):
    """Play one match, log the matchup and its result, and return
    (winner, loser). Shared by both bracket formats.
    """

    log.write(
        f"  {deck_a['name']} vs {deck_b['name']} "
        f"({games} games)... ",
        end="",
    )

    result = play_match(deck_a, deck_b, games)

    if result["deck_a_wins"] > result["deck_b_wins"]:

        winner, loser = deck_a, deck_b

    elif result["deck_b_wins"] > result["deck_a_wins"]:

        winner, loser = deck_b, deck_a

    else:

        winner, loser = rng.sample([deck_a, deck_b], 2)

        log.write("(tied — coin flip) ", end="")

    log.write(
        f"{winner['name']} wins "
        f"({result['deck_a_wins']}-{result['deck_b_wins']})"
    )

    if result.get("note"):

        log.write(f"    note: {result['note']}")

    return winner, loser


def run_round(decks, round_num, games, rng, log):

    log.write(f"\n=== Round {round_num} ({len(decks)} decks) ===")

    winners = []
    losers = []

    pairs = list(zip(decks[0::2], decks[1::2]))

    bye = decks[-1] if len(decks) % 2 else None

    for deck_a, deck_b in pairs:

        winner, loser = play_and_log_match(deck_a, deck_b, games, rng, log)

        winners.append(winner)
        losers.append(loser)

    if bye:

        log.write(f"  {bye['name']} gets a bye")

        winners.append(bye)

    return winners, losers


def make_pairings(pool, played_pairs, rng):
    """Pair up decks for one round, preferring an opponent each deck
    hasn't already played when there's any alternative — so the same
    two decks aren't forced to replay each other while other options
    exist. Returns (pairs, bye_deck_or_None).
    """

    remaining = list(pool)

    rng.shuffle(remaining)

    bye = remaining.pop() if len(remaining) % 2 else None

    pairs = []

    while remaining:

        deck_a = remaining.pop()

        partner_index = next(
            (
                i
                for i, deck_b in enumerate(remaining)
                if frozenset({deck_a["name"], deck_b["name"]})
                not in played_pairs
            ),
            0,
        )

        deck_b = remaining.pop(partner_index)

        pairs.append((deck_a, deck_b))

    return pairs, bye


def run_double_elim(decks, games, rng, log):
    """Double-elimination-flavored format: every deck can lose once
    before being eliminated (out on a 2nd loss). See the module
    docstring for why this is a simplified shared-pool model rather
    than a formally-seeded winners/losers bracket.

    Returns (champion, eliminated_at) to match the single-elim shape
    main() expects for printing standings.
    """

    losses = {deck["name"]: 0 for deck in decks}

    played_pairs = set()

    eliminated_at = {}

    round_num = 1

    while True:

        pool = [deck for deck in decks if losses[deck["name"]] < 2]

        if len(pool) <= 1:

            break

        log.write(
            f"\n=== Round {round_num} "
            f"({len(pool)} decks alive — 2 losses and you're out) ==="
        )

        pairs, bye = make_pairings(pool, played_pairs, rng)

        for deck_a, deck_b in pairs:

            winner, loser = play_and_log_match(
                deck_a, deck_b, games, rng, log
            )

            played_pairs.add(frozenset({deck_a["name"], deck_b["name"]}))

            losses[loser["name"]] += 1

            if losses[loser["name"]] >= 2:

                eliminated_at[loser["name"]] = round_num

                log.write(f"    {loser['name']} is eliminated (2nd loss)")

            else:

                log.write(f"    {loser['name']} drops to 1 loss")

        if bye:

            log.write(f"  {bye['name']} gets a bye")

        round_num += 1

    survivors = [deck for deck in decks if losses[deck["name"]] < 2]

    champion = survivors[0] if survivors else None

    return champion, eliminated_at


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

    parser.add_argument(
        "--format",
        choices=["single", "double-elim"],
        default="single",
        help=(
            "single = standard single-elimination. double-elim = "
            "everyone can lose once before being eliminated. "
            "(default: %(default)s)"
        ),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=RESULTS_DIR,
        help=(
            "Directory to save the run summary file in "
            "(default: %(default)s)"
        ),
    )

    args = parser.parse_args()

    log = RunLog()

    run_started_at = datetime.now()

    summary_path = (
        args.output_dir
        / f"bracket_{args.format}_{run_started_at:%Y%m%d_%H%M%S}.txt"
    )

    decks = load_decks()

    if len(decks) < 2:

        log.write(
            f"Need at least 2 exported decks in {DECKS_DIR} — found "
            f"{len(decks)}. Use \"Export All to Bracket Folder\" on "
            f"mtg-tracker's Decks page first."
        )

        log.save(summary_path)

        sys.exit(1)

    rng = random.Random(args.seed)

    rng.shuffle(decks)

    all_decks = list(decks)

    log.write(
        f"Bracket with {len(decks)} decks, {args.games} games per "
        f"matchup, format={args.format}:"
    )

    for deck in decks:

        log.write(f"  - {deck['name']}")

    champion = None

    eliminated_at = {}

    round_num = 1

    try:

        if args.format == "double-elim":

            champion, eliminated_at = run_double_elim(
                decks, args.games, rng, log
            )

        else:

            while len(decks) > 1:

                decks, round_losers = run_round(
                    decks, round_num, args.games, rng, log
                )

                for loser in round_losers:

                    eliminated_at[loser["name"]] = round_num

                round_num += 1

            champion = decks[0]

    except (requests.exceptions.RequestException, RuntimeError) as error:

        log.write(f"\nBracket aborted: {error}")

        log.save(summary_path)

        print(f"\nPartial results saved to {summary_path}")

        sys.exit(1)

    log.write(f"\n🏆 Champion: {champion['name']}\n")

    log.write("Final standings:")

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

        log.write(f"  {position}. {deck['name']} — {note}")

    log.save(summary_path)

    print(f"\nSummary saved to {summary_path}")


if __name__ == "__main__":

    main()
