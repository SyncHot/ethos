#!/bin/bash
# Get a compact overview of a file: functions, classes, routes, imports.
# Usage: file_overview.sh <file>
# Saves the agent from reading an entire 2000-line file just to find structure.
set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: file_overview.sh <file>"
    exit 1
fi

FILE="$1"
if [[ ! -f "$FILE" ]]; then
    echo "File not found: $FILE"
    exit 1
fi

LINES=$(wc -l < "$FILE")
echo "=== $FILE ($LINES lines) ==="

if [[ "$FILE" == *.py ]]; then
    echo ""
    echo "--- Imports ---"
    grep -n '^import \|^from ' "$FILE" | head -30

    echo ""
    echo "--- Classes ---"
    grep -n '^class ' "$FILE" || echo "(none)"

    echo ""
    echo "--- Functions/Methods ---"
    grep -n '^\( *\)def ' "$FILE" | head -60

    echo ""
    echo "--- Routes ---"
    grep -n '@.*\.route\|@.*\.get\|@.*\.post\|@.*\.put\|@.*\.delete' "$FILE" 2>/dev/null | head -30 || true

    echo ""
    echo "--- Constants/Globals ---"
    grep -n '^[A-Z_]\{2,\} *=' "$FILE" | head -20

elif [[ "$FILE" == *.js ]]; then
    echo ""
    echo "--- Imports ---"
    grep -n '^import ' "$FILE" | head -20

    echo ""
    echo "--- Functions ---"
    grep -nE '^\s*(function |const \w+ = |let \w+ = |async function |export )' "$FILE" | head -60

    echo ""
    echo "--- Event Listeners ---"
    grep -n 'addEventListener\|\.on(' "$FILE" | head -20

    echo ""
    echo "--- API calls ---"
    grep -n 'fetch(\|axios\.\|XMLHttpRequest\|\.ajax(' "$FILE" | head -20

elif [[ "$FILE" == *.html ]]; then
    echo ""
    echo "--- Script tags ---"
    grep -n '<script' "$FILE" | head -20

    echo ""
    echo "--- IDs ---"
    grep -oE 'id="[^"]+"' "$FILE" | sort -u | head -30
else
    echo "(unsupported file type — showing first 50 lines)"
    head -50 "$FILE"
fi
