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
VENV_TARGET="$VENV"
if [ -L "$VENV" ]; then
    VENV_TARGET="$(readlink "$VENV")"
fi

if [ ! -f "$VENV/bin/python" ]; then
    echo "  Creating venv..."
    # venv may be a symlink to /mnt/data/ethos/venv.  Python's venv module
    # chokes on dangling symlinks (EEXIST), so resolve the real target path
    # and create the venv there directly.
    if [ -L "$VENV" ]; then
        mkdir -p "$(dirname "$VENV_TARGET")"
        rm -rf "$VENV_TARGET"
    elif [ -d "$VENV" ]; then
        rm -rf "$VENV"
    fi
    python3 -m venv "$VENV_TARGET"
    echo "  Venv created at $VENV_TARGET"
else
    echo "  Venv exists at $VENV_TARGET"
fi

# Always ensure requirements are installed (handles interrupted first-boot)
echo "  Installing/verifying Python requirements..."
"$VENV/bin/pip" install --quiet --upgrade pip 2>&1 | tail -3 || true
PIP_CACHE="$ETHOS_DIR/.pip-cache"
if [ -f "$ETHOS_DIR/backend/requirements.txt" ]; then
    if [ -d "$PIP_CACHE" ] && ls "$PIP_CACHE"/*.whl &>/dev/null; then
        echo "  Using cached wheels (offline install)..."
        # --no-build-isolation: use venv setuptools for sdist packages (e.g. GPUtil)
        # || true: some optional packages may fail to build offline; critical check below catches real failures
        "$VENV/bin/pip" install --quiet --no-build-isolation --no-index --find-links "$PIP_CACHE" \
            -r "$ETHOS_DIR/backend/requirements.txt" 2>&1 | tail -5 || true
    else
        echo "  No wheel cache — installing from network..."
        "$VENV/bin/pip" install --quiet -r "$ETHOS_DIR/backend/requirements.txt" 2>&1 | tail -5 || true
    fi
fi

# Verify critical modules are importable
if ! "$VENV/bin/python" -c "import flask; import gevent; import psutil" 2>/dev/null; then
    echo "  WARNING: Critical modules missing, retrying from network..."
    "$VENV/bin/pip" install --no-cache-dir -r "$ETHOS_DIR/backend/requirements.txt" 2>&1 | tail -10
    # Final verification
    if ! "$VENV/bin/python" -c "import flask; import gevent" 2>/dev/null; then
        echo "  ERROR: Python requirements install failed!"
        exit 1
    fi
fi
echo "  Python environment ready."

echo "[4/8] Creating EthOS groups..."
getent group ethos-admin &>/dev/null || groupadd ethos-admin
getent group ethos-user  &>/dev/null || groupadd ethos-user
getent group ethos-family &>/dev/null || groupadd ethos-family
# Ensure the main user belongs to all required groups
usermod -aG sudo,ethos-admin,ethos-user "$ETHOS_USER" 2>/dev/null || true
echo "  Groups ready."

echo "[5/8] Setting hostname: $ETHOS_HOSTNAME..."
hostnamectl set-hostname "$ETHOS_HOSTNAME" 2>/dev/null || true

echo "[6/8] Deploying systemd services..."
# Write correct ethos.service with the configured port
cat > /etc/systemd/system/ethos.service << SVCEOF
[Unit]
Description=EthOS NAS
After=network.target ethos-firstboot.service local-fs.target
Wants=network.target
RequiresMountsFor=/mnt/data

[Service]
Type=notify
NotifyAccess=all
WorkingDirectory=/opt/ethos
EnvironmentFile=/opt/ethos/ethos.env
ExecStartPre=/bin/bash -c 'for d in data logs backups uploads venv; do p="/opt/ethos/\$d"; [ -L "\$p" ] && mkdir -p "\$(readlink "\$p")" || mkdir -p "\$p"; done'
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
# Disable firstboot service
systemctl disable ethos-firstboot.service 2>/dev/null || true

echo "[7/8] Regenerating SSH keys..."
rm -f /etc/ssh/ssh_host_* 2>/dev/null || true
ssh-keygen -A 2>/dev/null || true
systemctl enable ssh 2>/dev/null || true

# OOBE: generate unique self-signed TLS certificate for HTTPS
SSL_DIR="${ETHOS_DIR}/data/ssl"
SSL_KEY="${SSL_DIR}/ethos.key"
SSL_CRT="${SSL_DIR}/ethos.crt"
if [ ! -f "$SSL_CRT" ] && command -v openssl >/dev/null 2>&1; then
    mkdir -p "$SSL_DIR"
    HOSTNAME_FQDN="${ETHOS_HOSTNAME:-ethos}.local"
    openssl req -x509 -newkey rsa:4096 -nodes \
        -keyout "$SSL_KEY" -out "$SSL_CRT" \
        -days 3650 \
        -subj "/CN=${HOSTNAME_FQDN}/O=EthOS/OU=Auto-generated" \
        -addext "subjectAltName=DNS:${HOSTNAME_FQDN},DNS:ethos.local,DNS:localhost" \
        2>/dev/null || true
    if [ -f "$SSL_CRT" ]; then
        chmod 600 "$SSL_KEY"
        chmod 644 "$SSL_CRT"
        # Register paths in ethos.env
        if ! grep -q "^SSL_CERT=" "${ETHOS_DIR}/ethos.env" 2>/dev/null; then
            echo "SSL_CERT=${SSL_CRT}" >> "${ETHOS_DIR}/ethos.env"
            echo "SSL_KEY=${SSL_KEY}"  >> "${ETHOS_DIR}/ethos.env"
        fi
        echo "  TLS certificate generated: ${SSL_CRT}"
    fi
fi

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

# ── E2E Validation: Boot Beacon ──────────────────────────────────────────────
# If ETHOS_BUILD_HOST is set (injected at build time for QA), POST a "I AM ALIVE"
# beacon back to the Builder host so automated tests can confirm successful boot.
if [[ -n "${ETHOS_BUILD_HOST:-}" ]]; then
    ETHOS_VERSION=$(grep '^VERSION_ID=' /etc/os-release 2>/dev/null | cut -d= -f2 | tr -d '"' || echo "unknown")
    BEACON_PAYLOAD="{\"build_id\":\"${ETHOS_BUILD_ID:-}\",\"hostname\":\"${ETHOS_HOSTNAME}\",\"version\":\"${ETHOS_VERSION}\",\"timestamp\":$(date +%s)}"
    # Retry up to 10 times (service may not be up immediately)
    for _try in $(seq 1 10); do
        if curl -sf --max-time 5 \
                -H "Content-Type: application/json" \
                -d "${BEACON_PAYLOAD}" \
                "${ETHOS_BUILD_HOST}/api/builder/beacon" > /dev/null 2>&1; then
            echo "  Boot beacon sent to ${ETHOS_BUILD_HOST} (attempt ${_try})"
            break
        fi
        sleep 6
    done
fi

echo ""
echo "=========================================="
echo " First boot complete! Starting EthOS..."
echo "=========================================="

# Start main service (--no-block avoids waiting: ethos.service has
# After=ethos-firstboot.service, so systemd auto-starts it when firstboot
# finishes. --no-block returns immediately; reset-failed clears any
# rate-limit failure from the race window before firstboot completed.)
systemctl reset-failed ethos.service 2>/dev/null || true
systemctl start --no-block ethos.service 2>/dev/null || true
