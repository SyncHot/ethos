#!/bin/bash
# Git context in one call — replaces 3-5 git commands agents run every time.
# Usage: git_summary.sh [ticket_id]
set -euo pipefail
ETHOS=/opt/ethos
cd "$ETHOS"

TID="${1:-}"

echo "=== Git Status ==="
git status --short 2>/dev/null || echo "(not a git repo)"

echo ""
echo "=== Recent Commits (last 10) ==="
git --no-pager log --oneline -10 2>/dev/null || echo "(no commits)"

if [[ -n "$TID" ]]; then
    echo ""
    echo "=== Commits for ticket $TID ==="
    git --no-pager log --oneline -20 2>/dev/null | grep "$TID" || echo "(none)"

    echo ""
    echo "=== Diff stat for $TID commits ==="
    HASHES=$(git --no-pager log --oneline -20 2>/dev/null | grep "$TID" | awk '{print $1}')
    if [[ -n "$HASHES" ]]; then
        OLDEST=$(echo "$HASHES" | tail -1)
        git --no-pager diff --stat "${OLDEST}~1..HEAD" 2>/dev/null || true
    fi
fi

echo ""
echo "=== Uncommitted Changes ==="
DIFF=$(git --no-pager diff --stat 2>/dev/null)
if [[ -n "$DIFF" ]]; then
    echo "$DIFF"
else
    echo "(clean working tree)"
fi

echo ""
echo "=== Staged Changes ==="
STAGED=$(git --no-pager diff --cached --stat 2>/dev/null)
if [[ -n "$STAGED" ]]; then
    echo "$STAGED"
else
    echo "(nothing staged)"
fi
