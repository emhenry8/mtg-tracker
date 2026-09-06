"""Bracket runner for decks exported by mtg-tracker.

Reads every .dck file from DECKS_DIR (mtg-tracker's "Export All to
Bracket Folder" button writes these), seeds a random bracket, and
plays each matchup through forge-sim's /simulate endpoint — the same
service the main app's Simulate page uses, already parallelized across
several Forge processes per match. Prints the bracket round by round
and a final standings list, and saves three files under RESULTS_DIR,
all sharing one timestamped name:
  bracket_lives{N}_{time}.txt   the same text that printed to the console
  bracket_lives{N}_{time}.json  every match/round/standing as data
  bracket_lives{N}_{time}.html  a self-contained report — open it in
                                 any browser, no server, no internet
                                 needed beyond loading its two Google
                                 fonts. Built from report_template.html
                                 with the JSON above baked in.

Each matchup is best-of-N (11 games by default) rather than a single
game, so one unlucky draw or mulligan doesn't knock a deck out of the
bracket — the deck with more wins across the N games advances.

--lives N controls how many losses a deck can take before being
eliminated:
  --lives 1  Standard single-elimination — one loss and you're out.
  --lives 2  Double-elimination-flavored — out on a 2nd loss, so one
             unlucky round against the eventual best deck doesn't end
             your run the way it does at --lives 1.
  --lives 3+ Same idea, more lives, even more spread before elimination
             — every extra life roughly doubles total match count for
             the same field, so weigh that against how long a run you
             want.
This is a shared-pool model, not a formally-seeded winners/losers
bracket with strict re-entry rules (that has real edge cases for
non-power-of-2 fields) — everyone able to keep playing is repaired
each round, avoiding a repeat matchup where possible, until only
enough decks remain under the life cap to call a champion.

Usage (from the repo root, with the stack already up):
    docker compose run --rm bracket
    docker compose run --rm bracket --games 31 --seed 42
    docker compose run --rm bracket --lives 3
"""

import argparse
import json
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

DEFAULT_LIVES = 1

REPORT_TEMPLATE_PATH = Path(__file__).parent / "report_template.html"

REPORT_DATA_START = "/*__BRACKET_DATA__*/"

REPORT_DATA_END = "/*__END_BRACKET_DATA__*/"

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


def play_and_log_match(deck_a, deck_b, games, round_num, rng, log, events):
    """Play one match, log the matchup and its result, record a
    structured event for the JSON export, and return (winner, loser).
    """

    log.write(
        f"  {deck_a['name']} vs {deck_b['name']} "
        f"({games} games)... ",
        end="",
    )

    result = play_match(deck_a, deck_b, games)

    tied = result["deck_a_wins"] == result["deck_b_wins"]

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

    events.append({
        "type": "match",
        "round": round_num,
        "deck_a": deck_a["name"],
        "deck_b": deck_b["name"],
        "deck_a_wins": result["deck_a_wins"],
        "deck_b_wins": result["deck_b_wins"],
        "winner": winner["name"],
        "loser": loser["name"],
        "tied": tied,
        "note": result.get("note"),
    })

    return winner, loser


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


def run_survival_bracket(decks, games, lives, rng, log, events):
    """Play until only one deck has fewer than `lives` losses. Every
    deck can lose up to (lives - 1) times before elimination —
    lives=1 is standard single-elimination, lives=2 is the
    double-elimination-flavored format, and so on. See the module
    docstring for why this is a shared-pool model rather than a
    formally-seeded winners/losers bracket.

    Returns (champion, eliminated_at).
    """

    losses = {deck["name"]: 0 for deck in decks}

    played_pairs = set()

    eliminated_at = {}

    round_num = 1

    while True:

        pool = [deck for deck in decks if losses[deck["name"]] < lives]

        if len(pool) <= 1:

            break

        log.write(
            f"\n=== Round {round_num} "
            f"({len(pool)} decks alive — {lives} loss"
            f"{'es' if lives != 1 else ''} and you're out) ==="
        )

        pairs, bye = make_pairings(pool, played_pairs, rng)

        for deck_a, deck_b in pairs:

            winner, loser = play_and_log_match(
                deck_a, deck_b, games, round_num, rng, log, events
            )

            played_pairs.add(frozenset({deck_a["name"], deck_b["name"]}))

            losses[loser["name"]] += 1

            if losses[loser["name"]] >= lives:

                eliminated_at[loser["name"]] = round_num

                log.write(
                    f"    {loser['name']} is eliminated "
                    f"({losses[loser['name']]} loss"
                    f"{'es' if losses[loser['name']] != 1 else ''})"
                )

            else:

                log.write(
                    f"    {loser['name']} drops to "
                    f"{losses[loser['name']]} loss"
                    f"{'es' if losses[loser['name']] != 1 else ''}"
                )

        if bye:

            log.write(f"  {bye['name']} gets a bye")

            events.append({
                "type": "bye",
                "round": round_num,
                "deck": bye["name"],
            })

        round_num += 1

    survivors = [deck for deck in decks if losses[deck["name"]] < lives]

    champion = survivors[0] if survivors else None

    return champion, eliminated_at


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Run a bracket between all decks exported by mtg-tracker."
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
        "--lives",
        type=int,
        default=DEFAULT_LIVES,
        help=(
            "Losses a deck can take before elimination. 1 = standard "
            "single-elimination, 2 = double-elimination-flavored, "
            "3+ = even more spread before elimination, at the cost of "
            "roughly doubling total matches per extra life. "
            "(default: %(default)s)"
        ),
    )

    parser.add_argument(
        "--output-dir",
        type=Path,
        default=RESULTS_DIR,
        help=(
            "Directory to save the run summary files in "
            "(default: %(default)s)"
        ),
    )

    args = parser.parse_args()

    if args.lives < 1:

        print("--lives must be at least 1")

        sys.exit(1)

    log = RunLog()

    events = []

    run_started_at = datetime.now()

    summary_stem = (
        f"bracket_lives{args.lives}_{run_started_at:%Y%m%d_%H%M%S}"
    )

    summary_path = args.output_dir / f"{summary_stem}.txt"

    json_path = args.output_dir / f"{summary_stem}.json"

    html_path = args.output_dir / f"{summary_stem}.html"

    def save_results(champion_name, eliminated_at, all_decks, aborted=None):

        standings = None

        if all_decks is not None:

            def sort_key(deck):

                if deck["name"] == champion_name:

                    return (float("inf"), deck["name"])

                return (
                    eliminated_at.get(deck["name"], 0),
                    deck["name"],
                )

            ordered = sorted(all_decks, key=sort_key, reverse=True)

            standings = [
                {
                    "position": position,
                    "name": deck["name"],
                    "status": (
                        "champion"
                        if deck["name"] == champion_name
                        else f"eliminated round "
                        f"{eliminated_at[deck['name']]}"
                    ),
                }
                for position, deck in enumerate(ordered, start=1)
            ]

        payload = {
            "started_at": run_started_at.isoformat(),
            "games_per_match": args.games,
            "lives": args.lives,
            "seed": args.seed,
            "decks": [deck["name"] for deck in (all_decks or [])],
            "events": events,
            "champion": champion_name,
            "standings": standings,
            "aborted": aborted,
        }

        json_path.parent.mkdir(parents=True, exist_ok=True)

        json_path.write_text(json.dumps(payload, indent=2) + "\n")

        if REPORT_TEMPLATE_PATH.exists():

            template = REPORT_TEMPLATE_PATH.read_text()

            start = template.index(REPORT_DATA_START) + len(REPORT_DATA_START)

            end = template.index(REPORT_DATA_END)

            html = (
                template[:start]
                + json.dumps(payload)
                + template[end:]
            )

            html_path.write_text(html)

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
        f"matchup, lives={args.lives}:"
    )

    for deck in decks:

        log.write(f"  - {deck['name']}")

    try:

        champion, eliminated_at = run_survival_bracket(
            decks, args.games, args.lives, rng, log, events
        )

    except (requests.exceptions.RequestException, RuntimeError) as error:

        log.write(f"\nBracket aborted: {error}")

        log.save(summary_path)

        save_results(None, {}, None, aborted=str(error))

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

    save_results(champion["name"], eliminated_at, all_decks)

    print(f"\nSummary saved to {summary_path}")

    print(f"Structured results saved to {json_path}")

    print(f"Open in a browser: {html_path}")


if __name__ == "__main__":

    main()
