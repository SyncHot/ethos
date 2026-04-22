#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  EthOS Boot Diagnostics
#  Run on the booted device:  sudo ethos-diag
#  Collects all info needed to debug boot/startup issues.
# ═══════════════════════════════════════════════════════════

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

section() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  $1${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
fail() { echo -e "  ${RED}✗${NC} $1"; }
warn() { echo -e "  ${YELLOW}!${NC} $1"; }
info() { echo -e "  ${CYAN}i${NC} $1"; }

# ── Header ──
echo ""
echo -e "${BOLD}  EthOS Boot Diagnostics — $(date)${NC}"
echo ""

# ── 1. System info ──
section "1. System Info"
info "Hostname: $(hostname)"
info "Uptime: $(uptime -p)"
info "Kernel: $(uname -r)"
info "Disk usage /: $(df -h / 2>/dev/null | awk 'NR==2{print $3"/"$2" ("$5")"}')"

# ── 2. Network ──
section "2. Network"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
if [[ -n "$IP" ]]; then
    ok "IP address: $IP"
else
    fail "No IP address"
fi

if ping -c 1 -W 3 8.8.8.8 &>/dev/null; then
    ok "Internet: reachable (8.8.8.8)"
else
    fail "Internet: NOT reachable"
fi

if ping -c 1 -W 3 1.1.1.1 &>/dev/null; then
    ok "DNS alt: reachable (1.1.1.1)"
else
    warn "DNS alt: not reachable (1.1.1.1)"
fi

echo ""
info "Network interfaces:"
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>/dev/null | while IFS=: read -r dev type state conn; do
    if [[ "$state" == "connected" ]]; then
        ok "  $dev ($type): connected → $conn"
    elif [[ "$state" == *"disconnected"* ]]; then
        warn "  $dev ($type): disconnected"
    else
        info "  $dev ($type): $state"
    fi
done

# WiFi saved connections
echo ""
info "Saved WiFi connections:"
nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep wifi | while IFS=: read -r name type; do
    info "  → $name"
done
WIFI_COUNT=$(nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep -c wifi || echo 0)
if [[ "$WIFI_COUNT" == "0" ]]; then
    warn "  No saved WiFi connections"
fi

# ── 3. Key files ──
section "3. Key Files"
FILES=(
    "/opt/ethos/.installed"
    "/opt/ethos/ethos.env"
    "/opt/ethos/backend/app.py"
    "/opt/ethos/venv/bin/python"
    "/opt/ethos/backend/requirements.txt"
    "/opt/ethos-firstboot.sh"
    "/etc/systemd/system/ethos.service"
    "/etc/systemd/system/ethos-firstboot.service"
    "/var/log/ethos-firstboot.log"
)
for f in "${FILES[@]}"; do
    if [[ -f "$f" ]]; then
        ok "$f"
    else
        fail "$f  (MISSING)"
    fi
done

# ── 4. Installed marker ──
section "4. Install Status"
if [[ -f /opt/ethos/.installed ]]; then
    ok "Installed marker exists:"
    cat /opt/ethos/.installed | while read -r line; do
        info "  $line"
    done
else
    warn "NOT installed (.installed marker absent)"
    info "Firstboot should run on next boot"
fi

# ── 5. Service statuses ──
section "5. Systemd Services"
SERVICES=(ethos ethos-firstboot)
for svc in "${SERVICES[@]}"; do
    UNIT="${svc}.service"
    if ! systemctl list-unit-files "$UNIT" &>/dev/null 2>&1; then
        fail "$UNIT — not found"
        continue
    fi
    
    ENABLED=$(systemctl is-enabled "$UNIT" 2>/dev/null || echo "unknown")
    ACTIVE=$(systemctl is-active "$UNIT" 2>/dev/null || echo "unknown")
    
    STATUS_LINE="$UNIT  [enabled=$ENABLED, active=$ACTIVE]"
    
    if [[ "$ACTIVE" == "active" || "$ACTIVE" == "activating" ]]; then
        ok "$STATUS_LINE"
    elif [[ "$ACTIVE" == "inactive" && "$ENABLED" == "enabled" ]]; then
        warn "$STATUS_LINE"
    elif [[ "$ACTIVE" == "failed" ]]; then
        fail "$STATUS_LINE"
    else
        info "$STATUS_LINE"
    fi
done

# ── 6. Port 9000 ──
section "6. Port 9000"
PORT_PID=$(fuser 9000/tcp 2>/dev/null || echo "")
if [[ -n "$PORT_PID" ]]; then
    ok "Port 9000 is in use (PID: $PORT_PID)"
    PNAME=$(ps -p $PORT_PID -o comm= 2>/dev/null || echo "?")
    PCMD=$(ps -p $PORT_PID -o args= 2>/dev/null | head -c 120 || echo "?")
    info "  Process: $PNAME"
    info "  Command: $PCMD"
else
    fail "Port 9000 is NOT listening"
fi

# Test HTTP response
CURL_OUT=$(curl -sf -m 5 -o /dev/null -w "%{http_code}" "http://localhost:9000/" 2>/dev/null || echo "000")
if [[ "$CURL_OUT" == "200" ]]; then
    ok "HTTP localhost:9000/ → 200 OK"
elif [[ "$CURL_OUT" != "000" ]]; then
    warn "HTTP localhost:9000/ → $CURL_OUT"
else
    fail "HTTP localhost:9000/ → no response"
fi

# Check if it's the real EthOS or preboot
SETUP_OUT=$(curl -sf -m 5 "http://localhost:9000/api/setup/status" 2>/dev/null || echo "")
if [[ -n "$SETUP_OUT" ]]; then
    ok "Real EthOS is running (/api/setup/status responds)"
    info "  Response: $(echo "$SETUP_OUT" | head -c 200)"
else
    PREBOOT_OUT=$(curl -sf -m 5 "http://localhost:9000/health" 2>/dev/null || echo "")
    if [[ -n "$PREBOOT_OUT" ]]; then
        ok "Preboot installer is running (/health responds)"
        info "  Response: $PREBOOT_OUT"
    else
        warn "Neither EthOS nor preboot responding on :9000"
    fi
fi

# ── 7. Firstboot log ──
section "7. Firstboot Log (last 30 lines)"
if [[ -f /var/log/ethos-firstboot.log ]]; then
    LINES=$(wc -l < /var/log/ethos-firstboot.log)
    info "Log file: $LINES lines total"
    echo ""
    tail -30 /var/log/ethos-firstboot.log | while read -r line; do
        echo "    $line"
    done
else
    warn "No firstboot log found"
fi

# ── 8. Service journals (recent) ──
section "8. Service Journals (last 15 lines each)"

for svc in ethos ethos-firstboot; do
    echo ""
    echo -e "  ${BOLD}── $svc ──${NC}"
    JOURNAL=$(journalctl -u "$svc" --no-pager -n 15 --no-hostname 2>/dev/null || echo "(no journal)")
    if [[ -n "$JOURNAL" && "$JOURNAL" != *"No entries"* && "$JOURNAL" != "(no journal)" ]]; then
        echo "$JOURNAL" | while read -r line; do
            echo "    $line"
        done
    else
        info "  (no journal entries)"
    fi
done

# ── 9. Environment ──
section "9. EthOS Environment"
if [[ -f /opt/ethos/ethos.env ]]; then
    ok "ethos.env:"
    cat /opt/ethos/ethos.env | while read -r line; do
        info "  $line"
    done
else
    fail "ethos.env not found"
fi

# ── 9b. Firewall ──
section "9b. Firewall (UFW)"
UFW_STATUS=$(sudo ufw status verbose 2>/dev/null || echo "unknown")
if echo "$UFW_STATUS" | grep -q "Status: active"; then
    warn "UFW is ACTIVE"
    echo "$UFW_STATUS" | while read -r line; do
        info "  $line"
    done
elif echo "$UFW_STATUS" | grep -q "Status: inactive"; then
    ok "UFW is inactive (expected during installer mode)"
else
    info "UFW status: $UFW_STATUS"
fi

# ── 10. Python venv check ──
section "10. Python Venv"
if [[ -x /opt/ethos/venv/bin/python ]]; then
    PYVER=$(/opt/ethos/venv/bin/python --version 2>&1)
    ok "Venv python: $PYVER"
    
    # Quick import test
    if /opt/ethos/venv/bin/python -c "import flask; import gevent" 2>/dev/null; then
        ok "Key packages importable (flask, gevent)"
    else
        fail "Cannot import key packages — try checking:"
        info "  /opt/ethos/venv/bin/pip list"
    fi
else
    fail "Venv python not found or not executable"
fi

# ── Summary ──
section "Summary"
echo ""

# Determine overall state
INSTALLED=false
NETWORK=false
PORT_OK=false
ETHOS_OK=false

[[ -f /opt/ethos/.installed ]] && INSTALLED=true
[[ -n "$IP" ]] && NETWORK=true
[[ -n "$PORT_PID" ]] && PORT_OK=true
[[ -n "$SETUP_OUT" ]] && ETHOS_OK=true

if $ETHOS_OK; then
    ok "EthOS is running and serving on :9000"
    info "Open http://${IP:-<your-ip>}:9000 in browser"
elif $PORT_OK && ! $ETHOS_OK; then
    warn "Something is on port 9000 but not responding to health check"
    info "Check: sudo journalctl -u ethos -n 50"
elif $INSTALLED && ! $PORT_OK; then
    fail "EthOS is installed but NOT running"
    info "Try: sudo systemctl restart ethos"
    info "Check: journalctl -u ethos -n 50"
elif ! $INSTALLED && $NETWORK; then
    warn "Not installed yet but network is available"
    info "Firstboot should run. Check: journalctl -u ethos-firstboot -f"
elif ! $INSTALLED && ! $NETWORK; then
    warn "Not installed, no network"
    info "Connect device to Ethernet or WiFi"
fi

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BOLD}  End of diagnostics${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
