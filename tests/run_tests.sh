#!/usr/bin/env bash
# Run the EthOS NAS integration test suite.
# Restarts the server first to ensure a clean DB pool.
set -euo pipefail
cd "$(dirname "$0")/.."

# Set credentials via env: ETHOS_USER / ETHOS_PASS
export ETHOS_USER="${ETHOS_USER:-admin}"
export ETHOS_PASS="${ETHOS_PASS:-}"

echo "Restarting EthOS server for clean test state..."
sudo systemctl restart ethos 2>/dev/null || { sudo /opt/ethos/stop.sh; sleep 1; sudo /opt/ethos/start.sh; }
sleep 3

exec /opt/ethos/venv/bin/python -m pytest tests/ -v "$@"
