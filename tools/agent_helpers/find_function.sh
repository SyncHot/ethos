#!/bin/bash
# Find function/class/method definitions across the codebase.
# Usage: find_function.sh <name> [backend|frontend|all]
# Example: find_function.sh safe_path → finds def safe_path(...)
# Example: find_function.sh handleUpload frontend → searches only JS files
set -euo pipefail
ETHOS=/opt/ethos

if [[ $# -lt 1 ]]; then
    echo "Usage: find_function.sh <name> [backend|frontend|all]"
    exit 1
fi

NAME="$1"
SCOPE="${2:-all}"

case "$SCOPE" in
    backend)
        echo "=== Python definitions matching '$NAME' ==="
        grep -rnE "^(    )?(def|class) .*${NAME}" "$ETHOS/backend/" "$ETHOS/tools/" 2>/dev/null | \
            sed "s|$ETHOS/||g" || echo "(no matches)"
        ;;
    frontend)
        echo "=== JS definitions matching '$NAME' ==="
        grep -rnE "(function |const |let |var |async ).*${NAME}" "$ETHOS/frontend/js/" 2>/dev/null | \
            sed "s|$ETHOS/||g" || echo "(no matches)"
        ;;
    all|*)
        echo "=== Python definitions matching '$NAME' ==="
        grep -rnE "^(    )?(def|class) .*${NAME}" "$ETHOS/backend/" "$ETHOS/tools/" 2>/dev/null | \
            sed "s|$ETHOS/||g" || echo "(no matches)"
        echo ""
        echo "=== JS definitions matching '$NAME' ==="
        grep -rnE "(function |const |let |var |async ).*${NAME}" "$ETHOS/frontend/js/" 2>/dev/null | \
            sed "s|$ETHOS/||g" || echo "(no matches)"
        ;;
esac
