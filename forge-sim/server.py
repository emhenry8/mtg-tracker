import asyncio
import os
import re
import signal
from pathlib import Path

from fastapi import FastAPI
from pydantic import BaseModel


FORGE_DIR = Path("/opt/forge")

FORGE_JAR = (
    FORGE_DIR
    / "forge-gui-desktop-2.0.14-jar-with-dependencies.jar"
)

# Confirmed empirically: Forge's "-D <path>" flag for a custom deck
# directory does not work — it always looks here regardless.
DECKS_DIR = Path("/root/.forge/decks/constructed")

# Games requested by one /simulate call are split across this many
# concurrent Forge processes ("xvfb-run -a" hands each its own X
# display automatically, so they don't collide there). Each is a full
# JVM, so this trades RAM/CPU for wall-clock time — tune to the host.
WORKER_COUNT = 8

# Per-worker timeout. Since each worker only plays its share of the
# total games, this budget doesn't grow with the games count the way
# a single-process timeout would.
SIM_TIMEOUT_SECONDS = 300


MATCH_RESULT_RE = re.compile(
    r"^Match Result: Ai\(1\)-.+?: (?P<wins_a>\d+) "
    r"Ai\(2\)-.+?: (?P<wins_b>\d+)\s*$"
)

GAME_DURATION_RE = re.compile(
    r"Game Result: Game \d+ ended in (?P<ms>\d+) ms"
)


app = FastAPI()


class SimulateRequest(BaseModel):
    deck_a_name: str
    deck_a_dck: str
    deck_b_name: str
    deck_b_dck: str
    games: int = 20


# Only one /simulate request's decks may occupy DECKS_DIR at a time —
# Forge ignores our attempts to point it at a per-request directory
# (see DECKS_DIR above), so concurrent requests have to take turns.
# Games *within* one request are still fully parallel: the deck files
# are written once and only read afterward, so the workers sharing
# them concurrently is safe.
simulation_lock = asyncio.Lock()


def safe_deck_filename(name: str, fallback: str) -> str:
    slug = (
        re.sub(r"[^A-Za-z0-9_-]+", "_", name.strip())
        .strip("_")
    )

    return (slug or fallback) + ".dck"


def split_games(total: int, workers: int):
    """Divide `total` games into up to `workers` roughly-equal chunks,
    e.g. split_games(10, 4) -> [3, 3, 2, 2]. Never returns more chunks
    than there are games (no point spinning up an idle worker)."""

    workers = max(1, min(workers, total))

    base, remainder = divmod(total, workers)

    return [
        base + (1 if i < remainder else 0)
        for i in range(workers)
    ]


async def run_forge_chunk(deck_a_file: str, deck_b_file: str, games: int, worker_id: int):

    proc = await asyncio.create_subprocess_exec(
        "xvfb-run", "-a",
        "java", "-Dsentry.dsn=", "-Xmx2048m",
        "-jar", str(FORGE_JAR),
        "sim", "-d", deck_a_file, deck_b_file,
        "-n", str(games), "-q",
        cwd=str(FORGE_DIR),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        # xvfb-run is a shell wrapper around Xvfb + java — killing just
        # that wrapper process on timeout leaves its JVM (and Xvfb)
        # children running indefinitely. Starting it as its own
        # session/process-group leader lets us kill the whole tree.
        start_new_session=True,
    )

    try:

        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=SIM_TIMEOUT_SECONDS,
        )

    except asyncio.TimeoutError:

        try:

            os.killpg(proc.pid, signal.SIGKILL)

        except ProcessLookupError:

            pass

        await proc.wait()

        return {
            "worker_id": worker_id,
            "timed_out": True,
            "output": "",
        }

    output = (
        stdout.decode(errors="replace")
        + "\n"
        + stderr.decode(errors="replace")
    )

    return {
        "worker_id": worker_id,
        "timed_out": False,
        "output": output,
    }


# Forge prints these on every run, for its whole bundled card database
# — not just the cards in the decks being simulated — so they're not a
# signal about this particular match. Mostly "A-" (Arena/Alchemy
# digital-only rebalances) that never had a paper printing to file
# under a real set. Stripped so they don't crowd real diagnostic lines
# out of the tail we keep.
NOISE_LINE_PATTERNS = (
    "was not assigned to any set. Adding it to UNKNOWN set",
    "dated in the future. All `upcoming` cards will be added to this set",
)


def strip_noise_lines(output: str):

    return [
        line
        for line in output.splitlines()
        if not any(
            pattern in line
            for pattern in NOISE_LINE_PATTERNS
        )
    ]


def parse_worker_output(output: str):

    lines = strip_noise_lines(output)

    match_lines = [
        match
        for match in (
            MATCH_RESULT_RE.match(line)
            for line in lines
        )
        if match
    ]

    if not match_lines:

        return None

    final = match_lines[-1]

    durations = [
        int(match.group("ms"))
        for match in (
            GAME_DURATION_RE.search(line)
            for line in lines
        )
        if match
    ]

    return {
        "wins_a": int(final.group("wins_a")),
        "wins_b": int(final.group("wins_b")),
        "durations": durations,
        "tail": "\n".join(lines[-20:]),
    }


@app.post("/simulate")
async def simulate(request: SimulateRequest):

    games = max(1, min(request.games, 200))

    chunks = split_games(games, WORKER_COUNT)

    async with simulation_lock:

        DECKS_DIR.mkdir(parents=True, exist_ok=True)

        # Clear stale decks so Forge only ever sees the two we care about.
        for existing in DECKS_DIR.glob("*.dck"):
            existing.unlink()

        deck_a_file = safe_deck_filename(request.deck_a_name, "deck_a")
        deck_b_file = safe_deck_filename(request.deck_b_name, "deck_b")

        if deck_a_file == deck_b_file:
            deck_b_file = "b_" + deck_b_file

        (DECKS_DIR / deck_a_file).write_text(request.deck_a_dck)
        (DECKS_DIR / deck_b_file).write_text(request.deck_b_dck)

        worker_results = await asyncio.gather(*[
            run_forge_chunk(deck_a_file, deck_b_file, chunk_games, worker_id)
            for worker_id, chunk_games in enumerate(chunks)
        ])

    wins_a = 0
    wins_b = 0
    durations = []
    tails = []
    timed_out_workers = 0
    unparseable_workers = 0

    for worker in worker_results:

        if worker["timed_out"]:

            timed_out_workers += 1

            continue

        parsed = parse_worker_output(worker["output"])

        if parsed is None:

            unparseable_workers += 1

            tails.append(
                f"[worker {worker['worker_id']}] could not parse output:\n"
                + "\n".join(strip_noise_lines(worker["output"])[-20:])
            )

            continue

        wins_a += parsed["wins_a"]
        wins_b += parsed["wins_b"]
        durations.extend(parsed["durations"])

        tails.append(
            f"[worker {worker['worker_id']}]\n{parsed['tail']}"
        )

    games_played = wins_a + wins_b

    raw_tail = "\n\n".join(tails)[-6000:]

    if games_played == 0:

        if timed_out_workers == len(chunks):

            return {
                "error": f"Simulation timed out after {SIM_TIMEOUT_SECONDS}s",
                "raw_tail": raw_tail,
            }

        return {
            "error": "Could not parse simulation output",
            "raw_tail": raw_tail,
        }

    result = {
        "deck_a_wins": wins_a,
        "deck_b_wins": wins_b,
        "games_played": games_played,
        "avg_game_ms":
            sum(durations) / len(durations)
            if durations
            else None,
        "raw_tail": raw_tail,
    }

    failed_workers = timed_out_workers + unparseable_workers

    if failed_workers:

        result["note"] = (
            f"{failed_workers} of {len(chunks)} worker(s) didn't finish "
            f"cleanly — showing results from the {games_played} games "
            f"that did."
        )

    return result
