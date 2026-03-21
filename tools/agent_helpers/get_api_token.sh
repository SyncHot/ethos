#!/bin/bash
# Get a valid API token for testing endpoints.
# Usage: get_api_token.sh → prints just the token
#        eval $(get_api_token.sh --export) → sets TOKEN env var
# Example: curl -H "Authorization: Bearer $(get_api_token.sh)" http://localhost:9000/api/shares
set -euo pipefail

RESP=$(curl -sf -X POST http://localhost:9000/api/auth/login \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"${ETHOS_COPILOT_USER:-nasadmin}\",\"password\":\"${ETHOS_COPILOT_PASS:-ethos}\"}" 2>/dev/null) || {
    echo "ERROR: Could not get token — is ethos running?" >&2
    exit 1
}

TOKEN=$(echo "$RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['token'])" 2>/dev/null) || {
    echo "ERROR: Unexpected response: $RESP" >&2
    exit 1
}

if [[ "${1:-}" == "--export" ]]; then
    echo "export TOKEN=$TOKEN"
else
    echo "$TOKEN"
fi
