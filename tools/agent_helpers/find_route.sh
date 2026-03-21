#!/bin/bash
# Find API routes matching a pattern.
# Usage: find_route.sh <pattern>
# Example: find_route.sh upload → finds all routes with "upload"
# Example: find_route.sh "GET.*shares" → finds GET routes with "shares"
set -euo pipefail
ETHOS=/opt/ethos

if [[ $# -lt 1 ]]; then
    echo "Usage: find_route.sh <pattern>"
    echo "Searches all Flask routes (app.py + blueprints) for matching endpoints."
    exit 1
fi

PATTERN="$1"

echo "=== Routes matching '$PATTERN' ==="
grep -rnE "@(app|[a-z_]+_bp)\.route.*${PATTERN}" "$ETHOS/backend/" 2>/dev/null | \
    sed "s|$ETHOS/||g" | sort || true

echo ""
echo "=== Function defs near matching routes ==="
grep -rnE "@(app|[a-z_]+_bp)\.(route|get|post|put|delete|patch).*${PATTERN}" "$ETHOS/backend/" -A1 2>/dev/null | \
    sed "s|$ETHOS/||g" || echo "(no matches)"
