#!/bin/bash
# Safe restart: preflight check → eventlog → restart → verify.
# Replaces the 4-step restart dance agents do manually.
# Usage: safe_restart.sh [ticket_id] [reason]
set -euo pipefail
ETHOS=/opt/ethos
TID="${1:-manual}"
REASON="${2:-code changes}"

echo "=== Step 1: Preflight check ==="
if ! python3 "$ETHOS/tools/preflight_check.py"; then
    echo ""
    echo "❌ PREFLIGHT FAILED — fix errors above before restarting."
    exit 1
fi

echo ""
echo "=== Step 2: Log restart to eventlog ==="
curl -sf -X POST http://localhost:9000/api/eventlog \
    -H 'Content-Type: application/json' \
    -d "{\"category\":\"system\",\"level\":\"warning\",\"message\":\"Restart ethos z ticket watchera\",\"detail\":{\"ticket_id\":\"$TID\",\"reason\":\"$REASON\"}}" \
    > /dev/null 2>&1 || echo "(eventlog unavailable — proceeding anyway)"

echo ""
echo "=== Step 3: Restart ethos service ==="
sudo systemctl restart ethos

echo ""
echo "=== Step 4: Verify service status ==="
sleep 2
if systemctl is-active --quiet ethos; then
    echo "✅ ethos service is running"
    # Quick health check
    HTTP_CODE=$(curl -sf -o /dev/null -w '%{http_code}' http://localhost:9000/api/health 2>/dev/null || echo "000")
    if [[ "$HTTP_CODE" == "200" ]]; then
        echo "✅ API health check passed (HTTP 200)"
    else
        echo "⚠️ API not responding yet (HTTP $HTTP_CODE) — may need a few more seconds"
    fi
else
    echo "❌ ethos service failed to start!"
    echo "Check: sudo journalctl -u ethos -n 20 --no-pager"
    exit 1
fi
