#!/bin/bash
# Print essential project info that agents constantly have to rediscover.
# Usage: project_info.sh
set -euo pipefail
ETHOS=/opt/ethos

echo "=== EthOS Project Info ==="
echo "Root:          $ETHOS"
echo "Server port:   9000 (NOT 5000)"
echo "API base:      http://localhost:9000/api"
echo "Frontend:      http://localhost:9000/"
echo "Backend:       Flask + Gunicorn (gevent)"
echo "Service:       ethos (systemctl restart ethos)"

echo ""
echo "=== File Ownership ==="
echo "Backend files owned by root — use 'sudo tee' for edits when EACCES occurs:"
echo "  Example: echo 'content' | sudo tee /opt/ethos/backend/file.py > /dev/null"
echo "  Or: sudo cp /tmp/file.py /opt/ethos/backend/file.py"
GIT_USER=$(stat -c '%U' .git 2>/dev/null || echo "nasadmin")
echo "Git runs as $GIT_USER — use 'sudo -u $GIT_USER git push' for push"

echo ""
echo "=== Service Status ==="
systemctl is-active ethos 2>/dev/null && echo "  ethos: RUNNING" || echo "  ethos: STOPPED"
curl -sf -o /dev/null -w "  API health: HTTP %{http_code}\n" http://localhost:9000/api/health 2>/dev/null || echo "  API health: NOT RESPONDING"

echo ""
echo "=== Key Paths ==="
echo "  Backend:     $ETHOS/backend/app.py (main), $ETHOS/backend/blueprints/"
echo "  Frontend JS: $ETHOS/frontend/js/"
echo "  Frontend CSS:$ETHOS/frontend/css/"
echo "  Data:        $ETHOS/data/"
echo "  Docs:        $ETHOS/docs/"
echo "  Tools:       $ETHOS/tools/"
echo "  Logs:        $ETHOS/logs/"
echo "  Config:      $ETHOS/ethos/ethos.env, $ETHOS/ethos/install.conf"

echo ""
echo "=== Blueprint Structure ==="
set +e
for bp in "$ETHOS/backend/blueprints/"*.py; do
    [[ -f "$bp" ]] || continue
    fname=$(basename "$bp")
    prefix=$(grep -o 'url_prefix=[^ ]*' "$bp" 2>/dev/null | head -1 | sed 's/url_prefix=//;s/[^a-zA-Z0-9/_-]//g')
    routes=$(grep -cE '@.*\.(route|get|post|put|delete)' "$bp" 2>/dev/null)
    [[ "$routes" == "0" || -z "$routes" ]] && continue
    printf "  %-28s (%s) — %s routes\n" "$fname" "${prefix:-no prefix}" "$routes"
done
set -e

echo ""
echo "=== Restart Shortcut ==="
echo "  Use: bash /opt/ethos/tools/agent_helpers/safe_restart.sh <ticket_id> <reason>"
echo "  This runs preflight → eventlog → restart → verify in one command."
