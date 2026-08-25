#!/usr/bin/env bash
# Exports every deck from mtg-tracker to the shared bracket folder,
# then runs a single-elimination bracket across them via forge-sim.
#
# Usage:
#   ./run-bracket.sh
#   ./run-bracket.sh --games 21 --seed 42
#
# Any arguments are passed straight through to bracket.py (see
# bracket/bracket.py --help for the full list).

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

echo "Exporting decks to the bracket folder..."

docker compose exec -T mtg-tracker python3 -c "
import urllib.request
req = urllib.request.Request(
    'http://localhost:8000/decks/export-all',
    data=b'',
    method='POST',
)
urllib.request.urlopen(req, timeout=30)
"

echo "Running bracket..."
echo

docker compose run --rm bracket "$@"
