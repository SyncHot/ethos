#!/bin/bash
# EthOS First Boot Script (v2)
# Runs once after initial system image boot.
# Handles: venv check, service deploy, network wait, .installed marker.
set -euo pipefail

LOG="/var/log/ethos-firstboot.log"
exec > >(tee -a "$LOG") 2>&1

echo "=========================================="
echo " EthOS First Boot — $(date)"
echo "=========================================="

ETHOS_DIR="/opt/ethos"
INSTALLED_MARKER="$ETHOS_DIR/.installed"
CONF="$ETHOS_DIR/install.conf"

if [ -f "$INSTALLED_MARKER" ]; then
    echo "[OK] Already installed. Exiting."
    exit 0
fi

# Load config
if [ -f "$CONF" ]; then
    # shellcheck disable=SC1090
    source "$CONF"
fi

ETHOS_USER="${ETHOS_USER:-nasadmin}"
ETHOS_HOSTNAME="${ETHOS_HOSTNAME:-ethos}"
ETHOS_NAS_NAME="${ETHOS_NAS_NAME:-EthOS}"
ETHOS_PORT="${ETHOS_PORT:-9000}"
ETHOS_SETUP_WIZARD="${ETHOS_SETUP_WIZARD:-yes}"

echo "[1/8] Detecting platform..."
ARCH=$(uname -m)
echo "  Architecture: $ARCH"

echo "[2/8] Waiting for network (up to 60s)..."
for i in $(seq 1 30); do
    if ping -c1 -W2 8.8.8.8 &>/dev/null || ping -c1 -W2 1.1.1.1 &>/dev/null; then
        echo "  Network OK (attempt $i)"
        break
    fi
    sleep 2
done

echo "[3/8] Checking Python venv..."
VENV="$ETHOS_DIR/venv"
if [ ! -f "$VENV/bin/python" ]; then
    echo "  Creating venv..."
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --quiet --upgrade pip
    if [ -f "$ETHOS_DIR/backend/requirements.txt" ]; then
        "$VENV/bin/pip" install --quiet -r "$ETHOS_DIR/backend/requirements.txt"
    fi
    echo "  Venv created."
else
    echo "  Venv exists."
fi

echo "[4/8] Creating EthOS groups..."
getent group ethos-admin &>/dev/null || groupadd ethos-admin
getent group ethos-user  &>/dev/null || groupadd ethos-user
getent group ethos-family &>/dev/null || groupadd ethos-family
echo "  Groups ready."

echo "[5/8] Setting hostname: $ETHOS_HOSTNAME..."
hostnamectl set-hostname "$ETHOS_HOSTNAME" 2>/dev/null || true

echo "[6/8] Deploying systemd services..."
# Write correct ethos.service with the configured port
cat > /etc/systemd/system/ethos.service << SVCEOF
[Unit]
Description=EthOS NAS
After=network.target
Wants=network.target

[Service]
Type=notify
NotifyAccess=all
WorkingDirectory=/opt/ethos
EnvironmentFile=/opt/ethos/ethos.env
ExecStartPre=/bin/mkdir -p /opt/ethos/data /opt/ethos/logs /opt/ethos/backups /opt/ethos/uploads
Environment=PYTHONPATH=/opt/ethos/backend
ExecStart=/opt/ethos/venv/bin/python /opt/ethos/backend/app.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SVCEOF
systemctl daemon-reload 2>/dev/null || true
# Enable main EthOS service
systemctl enable ethos.service 2>/dev/null || true
# Disable installer services
systemctl disable ethos-preboot.service 2>/dev/null || true
systemctl disable ethos-firstboot.service 2>/dev/null || true

echo "[7/8] Regenerating SSH keys..."
rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
ssh-keygen -A 2>/dev/null || true
systemctl enable ssh 2>/dev/null || true

echo "[8/8] Marking installation complete..."
# Write ETHOS_USER to ethos.env so backend knows the correct user
if ! grep -q "^ETHOS_USER=" "$ETHOS_DIR/ethos.env" 2>/dev/null; then
    echo "ETHOS_USER=${ETHOS_USER}" >> "$ETHOS_DIR/ethos.env"
fi
# Create setup_done if wizard is disabled (user configured during install)
if [[ "$ETHOS_SETUP_WIZARD" != "yes" ]]; then
    mkdir -p "$ETHOS_DIR/data"
    echo "{\"timestamp\":$(date +%s),\"hostname\":\"${ETHOS_HOSTNAME}\",\"username\":\"${ETHOS_USER}\",\"nas_name\":\"${ETHOS_NAS_NAME}\"}" > "$ETHOS_DIR/data/setup_done"
    echo "installer" > "$ETHOS_DIR/.password_changed"
    echo "  Setup wizard skipped (pre-configured)."
fi
echo "installed $(date -Iseconds)" > "$INSTALLED_MARKER"

echo ""
echo "=========================================="
echo " First boot complete! Starting EthOS..."
echo "=========================================="

# Start main service
systemctl start ethos.service 2>/dev/null || true
