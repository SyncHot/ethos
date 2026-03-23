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

echo "[4/8] Creating user $ETHOS_USER..."
if ! id "$ETHOS_USER" &>/dev/null; then
    useradd -m -s /bin/bash -G sudo "$ETHOS_USER" 2>/dev/null || true
    echo "${ETHOS_USER}:ethos" | chpasswd
    echo "  User created."
else
    echo "  User already exists."
fi

echo "[5/8] Setting hostname: $ETHOS_HOSTNAME..."
hostnamectl set-hostname "$ETHOS_HOSTNAME" 2>/dev/null || true

echo "[6/8] Deploying systemd services..."
# Enable main EthOS service
systemctl enable ethos.service 2>/dev/null || true
# Disable installer services
systemctl disable ethos-preboot.service 2>/dev/null || true
systemctl disable ethos-firstboot.service 2>/dev/null || true

echo "[7/8] Regenerating SSH keys..."
rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
ssh-keygen -A 2>/dev/null || true

echo "[8/8] Marking installation complete..."
echo "installed $(date -Iseconds)" > "$INSTALLED_MARKER"

echo ""
echo "=========================================="
echo " First boot complete! Starting EthOS..."
echo "=========================================="

# Start main service
systemctl start ethos.service 2>/dev/null || true
