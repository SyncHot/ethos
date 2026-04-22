#!/usr/bin/env bash
# DEPRECATED: Use firstboot-v2.sh instead. This file will be removed in a future release.
# ═══════════════════════════════════════════════════════════
#  EthOS — First Boot Script
#  Runs once on first startup:
#   • Pre-packaged mode: app already in /opt/ethos (built images)
#   • Installer mode:    app in /opt/ethos-installer/app (RPi)
#  Logs to /var/log/ethos-firstboot.log
# ═══════════════════════════════════════════════════════════
set -euo pipefail

LOG="/var/log/ethos-firstboot.log"
INSTALL_DIR="/opt/ethos"
INSTALLER_DIR="/opt/ethos-installer"
MARKER="$INSTALL_DIR/.installed"

exec > >(tee -a "$LOG") 2>&1

echo "═══════════════════════════════════════════"
echo "  EthOS First Boot — $(date)"
echo "═══════════════════════════════════════════"

# ─── Expand root partition to fill entire disk ───
# The image is ~4 GB but the target disk is usually much larger.
# We grow the root partition (p2 on RPi, p3 on x86) and resize the filesystem.
# Only runs once — tracked by .resized marker.
RESIZED_MARKER="/opt/ethos/.resized"
if [[ -f "$RESIZED_MARKER" ]]; then
    echo "[✓] Partycja już rozszerzona (marker istnieje)"
else
    echo "[i] Sprawdzam czy partycja wymaga rozszerzenia..."
    ROOT_DEV=$(findmnt -n -o SOURCE /)
    if [[ -n "$ROOT_DEV" ]]; then
        # Extract disk device and partition number (e.g., /dev/sda3 → /dev/sda + 3)
        DISK_DEV=$(lsblk -no PKNAME "$ROOT_DEV" 2>/dev/null | head -1)
        if [[ -n "$DISK_DEV" ]]; then
            DISK_DEV="/dev/$DISK_DEV"
            PART_NUM=$(echo "$ROOT_DEV" | grep -oE '[0-9]+$')
            if [[ -n "$PART_NUM" ]]; then
                DISK_SIZE=$(blockdev --getsize64 "$DISK_DEV" 2>/dev/null || echo 0)
                PART_SIZE=$(blockdev --getsize64 "$ROOT_DEV" 2>/dev/null || echo 0)
                # If partition uses less than 90% of disk, expand it
                if (( DISK_SIZE > 0 && PART_SIZE > 0 && PART_SIZE * 100 / DISK_SIZE < 90 )); then
                    echo "[i] Dysk: $(numfmt --to=iec $DISK_SIZE), partycja root: $(numfmt --to=iec $PART_SIZE)"
                    echo "[i] Rozszerzam partycję $ROOT_DEV na cały dysk..."
                    if command -v growpart &>/dev/null; then
                        growpart "$DISK_DEV" "$PART_NUM" 2>&1 || echo "[!] growpart warning (may already be max)"
                    else
                        # Fallback: use parted to resize
                        parted -s "$DISK_DEV" resizepart "$PART_NUM" 100% 2>&1 || echo "[!] parted resizepart warning"
                    fi
                    # Ensure kernel sees the new partition size
                    partprobe "$DISK_DEV" 2>/dev/null || true
                    sleep 2
                    echo "[i] Rozszerzam filesystem..."
                    resize2fs "$ROOT_DEV" 2>&1 || echo "[!] resize2fs warning"
                    # Sync filesystem metadata to disk
                    sync; sync
                    sleep 1
                    NEW_SIZE=$(blockdev --getsize64 "$ROOT_DEV" 2>/dev/null || echo 0)
                    echo "[✓] Partycja rozszerzona: $(numfmt --to=iec $PART_SIZE) → $(numfmt --to=iec $NEW_SIZE)"
                    # Force fsck on next boot to clean up metadata after resize
                    if [ -f /etc/default/grub ]; then
                        # x86: inject fsck.mode=force into GRUB cmdline
                        if ! grep -q 'fsck.mode=force' /etc/default/grub 2>/dev/null; then
                            sed -i 's/GRUB_CMDLINE_LINUX_DEFAULT="/GRUB_CMDLINE_LINUX_DEFAULT="fsck.mode=force /' /etc/default/grub
                            update-grub 2>/dev/null || true
                        fi
                    elif [ -f /boot/firmware/cmdline.txt ]; then
                        # RPi: inject fsck.mode=force into kernel cmdline
                        if ! grep -q 'fsck.mode=force' /boot/firmware/cmdline.txt 2>/dev/null; then
                            sed -i 's/$/ fsck.mode=force/' /boot/firmware/cmdline.txt
                        fi
                    elif [ -f /boot/cmdline.txt ]; then
                        # RPi (old layout): inject into /boot/cmdline.txt
                        if ! grep -q 'fsck.mode=force' /boot/cmdline.txt 2>/dev/null; then
                            sed -i 's/$/ fsck.mode=force/' /boot/cmdline.txt
                        fi
                    fi
                    # Also set tune2fs as fallback
                    tune2fs -C 100 -c 1 "$ROOT_DEV" 2>/dev/null || true
                    echo "[i] Wymuszono fsck przy następnym uruchomieniu"
                else
                    echo "[✓] Partycja root już wykorzystuje cały dysk"
                fi
            fi
        fi
    else
        echo "[!] Nie mogę ustalić urządzenia root — pomijam rozszerzanie"
    fi
    # Mark resize as done (even if no resize needed) so we don't check again
    mkdir -p /opt/ethos
    touch "$RESIZED_MARKER"
fi

# Already installed?
if [[ -f "$MARKER" ]]; then
    echo "[i] EthOS already installed, skipping."
    systemctl disable ethos-firstboot.service 2>/dev/null || true
    exit 0
fi

# [DIST-SEC] Regenerate SSH host keys if missing (security fix)
# Removed by builder to ensure unique keys per installation
if ! ls /etc/ssh/ssh_host_* 1> /dev/null 2>&1; then
    echo "[i] Regeneracja kluczy SSH..."
    DEBIAN_FRONTEND=noninteractive dpkg-reconfigure openssh-server
    # Ensure they serve immediately
    systemctl restart ssh
    echo "[✓] Klucze SSH wygenerowane"
fi
systemctl enable ssh 2>/dev/null || true

# ─── Detect mode ───
# Pre-packaged: config in /opt/ethos/install.conf, backend already present
# Installer:    config in /opt/ethos-installer/install.conf, needs copy
if [[ -f "$INSTALL_DIR/install.conf" ]]; then
    CONF="$INSTALL_DIR/install.conf"
    MODE="prepackaged"
elif [[ -f "$INSTALLER_DIR/install.conf" ]]; then
    CONF="$INSTALLER_DIR/install.conf"
    MODE="installer"
else
    CONF=""
    MODE="prepackaged"
fi

# Load config (or defaults)
if [[ -n "$CONF" && -f "$CONF" ]]; then
    # shellcheck source=/dev/null
    source "$CONF"
else
    echo "[!] No config file found, using defaults"
fi
ETHOS_USER="${ETHOS_USER:-nasadmin}"
ETHOS_HOSTNAME="${ETHOS_HOSTNAME:-ethos}"
ETHOS_NAS_NAME="${ETHOS_NAS_NAME:-EthOS}"
ETHOS_PORT="${ETHOS_PORT:-9000}"
ETHOS_SETUP_WIZARD="${ETHOS_SETUP_WIZARD:-yes}"

echo "[i] Mode: $MODE"
echo "[i] Config loaded:"
echo "    User:     ${ETHOS_USER}"
echo "    Hostname: ${ETHOS_HOSTNAME}"
echo "    NAS Name: ${ETHOS_NAS_NAME}"
echo "    Port:     ${ETHOS_PORT}"

# ─── Wait for network ───
# No network → exit and let preboot-server handle WiFi setup.
# After WiFi is configured, system reboots and firstboot runs again with network.
# Determine wait time: if saved WiFi exists, wait longer (AP race condition).
WAIT_SECS=30
if nmcli -t -f TYPE,NAME connection show 2>/dev/null | grep -q '^802-11-wireless:' \
   && ! nmcli -t -f TYPE,NAME connection show 2>/dev/null | grep -q 'ethos-hotspot'; then
    WAIT_SECS=90
    echo "[i] Zapisane WiFi wykryte — wydłużam oczekiwanie na sieć"
fi
NETWORK_OK=false
echo "[i] Sprawdzam sieć (${WAIT_SECS}s)..."
WAIT_ITERS=$(( WAIT_SECS / 2 ))
for i in $(seq 1 "$WAIT_ITERS"); do
    # Try ping first (fast), then HTTP as fallback (works in QEMU user-mode
    # and networks that block ICMP)
    if ping -c 1 -W 2 8.8.8.8 &>/dev/null || ping -c 1 -W 2 1.1.1.1 &>/dev/null; then
        echo "[✓] Sieć dostępna (ping)"
        NETWORK_OK=true
        break
    elif curl -sf --max-time 3 --connect-timeout 3 http://deb.debian.org/debian/dists/bookworm/Release.gpg -o /dev/null 2>/dev/null; then
        echo "[✓] Sieć dostępna (HTTP — ICMP zablokowany)"
        NETWORK_OK=true
        break
    elif ip route show default 2>/dev/null | grep -q default; then
        # Has default route — try direct TCP connect to well-known port
        if timeout 3 bash -c 'echo > /dev/tcp/8.8.8.8/53' 2>/dev/null; then
            echo "[✓] Sieć dostępna (TCP)"
            NETWORK_OK=true
            break
        fi
    fi
    sleep 2
done
if ! $NETWORK_OK; then
    echo "[i] Brak sieci — oczekuję konfiguracji WiFi"
    echo "[i] Połącz się z WiFi 'ethos' (bez hasła)"
    echo "[i] i otwórz http://192.168.42.1:9000"
    echo "[i] System uruchomi się ponownie po skonfigurowaniu WiFi"
    exit 0
fi

export DEBIAN_FRONTEND=noninteractive

# ─── Pre-packaged fast path ───
# Builder already installed ALL system deps, python venv, services etc.
# Skip slow dpkg -s loops and apt-get calls entirely.
PREPACKAGED_VENV="$INSTALL_DIR/venv/bin/python"
if [[ "$MODE" == "prepackaged" && -f "$PREPACKAGED_VENV" ]]; then
    echo "[✓] Tryb prepackaged — pomijam instalację zależności (wszystko wbudowane w obraz)"

    # Only ensure NM config exists (fast — single file check)
    mkdir -p /etc/NetworkManager/conf.d
    if [[ ! -f /etc/NetworkManager/conf.d/00-ethos.conf ]]; then
        cat > /etc/NetworkManager/conf.d/00-ethos.conf <<'NMCFG'
[main]
dns=dnsmasq

[device]
wifi.scan-rand-mac-address=no
NMCFG
        echo "[✓] NetworkManager config"
        systemctl restart NetworkManager 2>/dev/null || true
    fi

    # Mask dnsmasq (fast)
    systemctl disable dnsmasq 2>/dev/null || true
    systemctl mask dnsmasq 2>/dev/null || true

else
    # ─── Installer / fallback mode: install everything ───
    echo "[i] Tryb instalacji — sprawdzam i instaluję zależności..."

    # ─── System dependencies (only install missing ones) ───
    # Minimal: avahi for .local discovery, WiFi stack for wireless-only devices.
    # Everything else (storage tools, sensors, printer, archives…) is installed
    # lazily by EthOS (ensure_dep) when the user enables a feature / app.
    echo "[i] Sprawdzam zależności systemowe..."
    DEPS_NEEDED=()
    DEPS=(
        network-manager avahi-daemon
        wpasupplicant dnsmasq rfkill
    )
    for dep in "${DEPS[@]}"; do
        dpkg -s "$dep" &>/dev/null || DEPS_NEEDED+=("$dep")
    done
    if (( ${#DEPS_NEEDED[@]} > 0 )); then
        echo "[i] Instaluję ${#DEPS_NEEDED[@]} brakujących pakietów..."
        apt-get update -qq 2>/dev/null || true
        apt-get install -y -qq "${DEPS_NEEDED[@]}" 2>/dev/null || {
            echo "[!] Niektóre pakiety mogły się nie zainstalować"
        }
    else
        echo "[✓] Wszystkie zależności zainstalowane"
    fi

    # Disable standalone dnsmasq
    systemctl disable dnsmasq 2>/dev/null || true
    systemctl mask dnsmasq 2>/dev/null || true

    # NetworkManager config — always (re)write after dnsmasq is installed
    # RPi image ships WITHOUT dns=dnsmasq (package unavailable at build time)
    mkdir -p /etc/NetworkManager/conf.d
    cat > /etc/NetworkManager/conf.d/00-ethos.conf <<'NMCFG'
[main]
dns=dnsmasq

[device]
wifi.scan-rand-mac-address=no
NMCFG
    echo "[✓] NetworkManager config (dnsmasq + WiFi scan)"
    systemctl restart NetworkManager 2>/dev/null || true

    # Python3 + venv
    NATIVE_DEPS=(python3 python3-pip python3-venv)
    for dep in "${NATIVE_DEPS[@]}"; do
        dpkg -s "$dep" &>/dev/null || {
            apt-get install -y -qq "$dep" 2>/dev/null || true
        }
    done

    echo "[✓] Zależności zainstalowane"
fi

# ─── Configure user groups ───
echo "[i] Konfiguruję grupy..."
getent group ethos-admin &>/dev/null || groupadd ethos-admin
getent group ethos-user &>/dev/null || groupadd ethos-user
getent group ethos-family &>/dev/null || groupadd ethos-family

EXTRA_GROUPS="ethos-admin,ethos-user,sudo"

if id "$ETHOS_USER" &>/dev/null; then
    usermod -aG "$EXTRA_GROUPS" "$ETHOS_USER"
    echo "[✓] Użytkownik $ETHOS_USER dodany do grup"
else
    echo "[i] Tworzę użytkownika $ETHOS_USER..."
    useradd -m -s /bin/bash -G "$EXTRA_GROUPS" "$ETHOS_USER"
    echo "${ETHOS_USER}:ethos" | chpasswd
    echo "[✓] Użytkownik $ETHOS_USER utworzony"
fi

# ─── Install EthOS ───
echo "[i] Przygotowuję EthOS..."
mkdir -p "$INSTALL_DIR"/{data,backups,logs,uploads,.thumb_cache,cups-config}

if [[ "$MODE" == "installer" ]]; then
    # Installer mode: copy from extracted app directory
    if [[ -d "$INSTALLER_DIR/app" ]]; then
        echo "[i] Kopiuję pliki aplikacji z instalatora..."
        cp -r "$INSTALLER_DIR/app/backend" "$INSTALL_DIR/"
        cp -r "$INSTALLER_DIR/app/frontend" "$INSTALL_DIR/"
        [[ -d "$INSTALLER_DIR/app/cups-config" ]] && \
            cp -r "$INSTALLER_DIR/app/cups-config/"* "$INSTALL_DIR/cups-config/" 2>/dev/null || true
    else
        echo "[✗] Brak katalogu app/ — instalacja niekompletna"
    fi
else
    # Pre-packaged mode: files already in /opt/ethos
    if [[ -d "$INSTALL_DIR/backend" ]]; then
        echo "[✓] Pliki aplikacji już na miejscu"
    else
        echo "[✗] BŁĄD: Brak plików aplikacji w $INSTALL_DIR"
        exit 1
    fi
fi

# ─── Deploy: Python venv + systemd ───
# ── Python venv (skip if pre-packaged and already exists) ──
if [[ -f "$INSTALL_DIR/venv/bin/python" ]]; then
    echo "[✓] Python venv już istnieje"
else
    echo "[i] Tworzę Python venv..."
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --no-cache-dir -r "$INSTALL_DIR/backend/requirements.txt"
    echo "[✓] Python venv gotowy"
fi

# ── Environment file (skip if pre-packaged) ──
if [[ ! -f "$INSTALL_DIR/ethos.env" ]]; then
    cat > "$INSTALL_DIR/ethos.env" <<ENVF
NAS_NAME=${ETHOS_NAS_NAME}
PORT=${ETHOS_PORT}
ETHOS_ROOT=${INSTALL_DIR}
ENVF
else
    echo "[✓] ethos.env już istnieje"
fi

# ── Systemd service (skip if pre-packaged) ──
if [[ ! -f /etc/systemd/system/ethos.service ]]; then
    cat > /etc/systemd/system/ethos.service <<SYSTEMD
[Unit]
Description=EthOS NAS
After=network.target
Wants=network.target

[Service]
Type=notify
NotifyAccess=all
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${INSTALL_DIR}/ethos.env
Environment=PYTHONPATH=${INSTALL_DIR}/backend
ExecStartPre=/bin/mkdir -p ${INSTALL_DIR}/data ${INSTALL_DIR}/logs ${INSTALL_DIR}/backups ${INSTALL_DIR}/uploads
ExecStart=${INSTALL_DIR}/venv/bin/python ${INSTALL_DIR}/backend/app.py
Restart=on-failure
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
SYSTEMD
else
    echo "[✓] ethos.service już istnieje"
fi

# ── Management scripts ──
if [[ ! -f "$INSTALL_DIR/start.sh" ]]; then
    cat > "$INSTALL_DIR/start.sh" <<'SH'
#!/bin/bash
sudo systemctl start ethos
echo "EthOS uruchomiony"
SH
    cat > "$INSTALL_DIR/stop.sh" <<'SH'
#!/bin/bash
sudo systemctl stop ethos
echo "EthOS zatrzymany"
SH
    cat > "$INSTALL_DIR/rebuild.sh" <<'SH'
#!/bin/bash
cd "$(dirname "$0")"
./venv/bin/pip install --quiet --no-cache-dir -r backend/requirements.txt
sudo systemctl restart ethos
echo "EthOS przebudowany i uruchomiony"
SH
    chmod +x "$INSTALL_DIR"/{start,stop,rebuild}.sh
fi

# ── Stop preboot server (frees port 9000) ──
systemctl stop ethos-preboot.service 2>/dev/null || true
systemctl disable ethos-preboot.service 2>/dev/null || true
# Kill any remaining process on port 9000
fuser -k 9000/tcp 2>/dev/null || true
sleep 1

# ── Start ──
systemctl daemon-reload
systemctl enable ethos.service
# Start with --no-block to avoid deadlock if After= ordering exists
systemctl restart --no-block ethos.service

# ── Wait for app to start ──
echo "[i] Czekam na uruchomienie..."
STARTED=false
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${ETHOS_PORT}/api/setup/status" &>/dev/null; then
        STARTED=true
        break
    fi
    sleep 2
done

if $STARTED; then
    echo "[✓] EthOS uruchomiony na porcie ${ETHOS_PORT}"
    if [[ "$ETHOS_SETUP_WIZARD" != "yes" ]]; then
        echo "{\"timestamp\":$(date +%s),\"hostname\":\"${ETHOS_HOSTNAME}\",\"username\":\"${ETHOS_USER}\",\"nas_name\":\"${ETHOS_NAS_NAME}\"}" > "$INSTALL_DIR/data/setup_done"
        echo "[✓] Setup wizard wyłączony (dane ustawione przy budowaniu)"
    else
        echo "[i] Setup wizard aktywny — skonfiguruj system w przeglądarce"
    fi
else
    echo "[!] EthOS może potrzebować więcej czasu — sprawdź: journalctl -u ethos"
fi

# ─── USB automount (devmon) ───
echo "[i] Konfiguruję USB automount (devmon)..."
if ! command -v devmon &>/dev/null && ! command -v udevil &>/dev/null; then
    apt-get install -y -qq udevil 2>/dev/null || true
fi

# Only set up the service if udevil/devmon actually installed
if command -v devmon &>/dev/null; then
    if ! id devmon &>/dev/null; then
        useradd -r -s /usr/sbin/nologin -d /media/devmon devmon 2>/dev/null || true
    fi
    mkdir -p /media/devmon
    chown devmon:root /media/devmon
    chmod 755 /media/devmon

    cat > /etc/systemd/system/devmon@.service <<'DEVMON'
[Unit]
Description=devmon USB automounter for %i
After=local-fs.target

[Service]
Type=simple
User=%i
ExecStart=/usr/bin/devmon --no-gui --sync
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
DEVMON
    systemctl daemon-reload
    systemctl enable --now devmon@devmon.service 2>/dev/null || true
    echo "[✓] USB automount skonfigurowany"
else
    echo "[!] udevil/devmon nie zainstalowany — USB automount niedostępny (zainstaluj z panelu EthOS)"
fi

# ─── Samba (opcjonalna — instalowana z panelu EthOS) ───
if command -v smbd &>/dev/null; then
    systemctl enable smbd 2>/dev/null || true
    systemctl start smbd 2>/dev/null || true
    echo "[✓] Samba uruchomiona"
else
    echo "[i] Samba nie zainstalowana — zainstaluj z panelu EthOS (Dyski → Samba)"
    # Mask nmbd to prevent failed service errors when samba is not installed
    systemctl mask nmbd 2>/dev/null || true
fi

# ─── Avahi (mDNS) ───
if command -v avahi-daemon &>/dev/null; then
    systemctl enable avahi-daemon 2>/dev/null || true
    systemctl start avahi-daemon 2>/dev/null || true
    echo "[✓] Avahi (mDNS) uruchomione"
fi

# ─── WiFi AP (hotspot) ───
# For prepackaged images, builder already created ethos-ap.service.
# Only configure for installer mode where service may not exist.
if [[ ! -f /etc/systemd/system/ethos-ap.service ]]; then
    echo "[i] Konfiguruję hotspot WiFi..."
    AP_SCRIPT=""
    if [[ -x "/usr/local/bin/ethos-ap" ]]; then
        AP_SCRIPT="/usr/local/bin/ethos-ap"
    elif [[ -f "$INSTALLER_DIR/ethos-ap.sh" ]]; then
        cp "$INSTALLER_DIR/ethos-ap.sh" "$INSTALL_DIR/ethos-ap.sh"
        chmod +x "$INSTALL_DIR/ethos-ap.sh"
        AP_SCRIPT="$INSTALL_DIR/ethos-ap.sh"
    fi

    if [[ -n "$AP_SCRIPT" ]]; then
        cat > /etc/systemd/system/ethos-ap.service <<APSVC
[Unit]
Description=EthOS WiFi Hotspot (auto-start if no network)
After=NetworkManager.service
Wants=NetworkManager.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash ${AP_SCRIPT} auto
ExecStop=/bin/bash ${AP_SCRIPT} stop

[Install]
WantedBy=multi-user.target
APSVC
        systemctl daemon-reload
        systemctl enable ethos-ap.service
        echo "[✓] Serwis auto-hotspot utworzony"
    else
        echo "[!] Brak skryptu ethos-ap — pomijam hotspot"
    fi
else
    echo "[✓] Serwis auto-hotspot już skonfigurowany"
fi

# ─── Mark as installed ───
touch "$MARKER"
echo "installed=$(date -Iseconds)" >> "$MARKER"
echo "user=$ETHOS_USER" >> "$MARKER"
echo "hostname=$ETHOS_HOSTNAME" >> "$MARKER"
echo "port=$ETHOS_PORT" >> "$MARKER"

# ─── Disable first-boot service ───
systemctl disable ethos-firstboot.service 2>/dev/null || true

# ─── Pre-boot already stopped above (before ethos.service start) ───
systemctl stop ethos-preboot.service 2>/dev/null || true
systemctl disable ethos-preboot.service 2>/dev/null || true

# ─── Cleanup installer files ───
if [[ -d "$INSTALLER_DIR/app" ]]; then
    rm -rf "$INSTALLER_DIR/app"
    echo "[i] Wyczyszczono pliki instalatora"
fi

# ─── Power Management ───
echo "[i] Konfiguruję zarządzanie energią..."

# Copy config script
if [ -f "$INSTALL_DIR/tools/ethos-power-config.sh" ]; then
    cp "$INSTALL_DIR/tools/ethos-power-config.sh" /usr/local/bin/ethos-power-config
    chmod +x /usr/local/bin/ethos-power-config
fi

# Copy systemd service
if [ -f "$INSTALL_DIR/tools/ethos-power.service" ]; then
    cp "$INSTALL_DIR/tools/ethos-power.service" /etc/systemd/system/ethos-power.service
    # Reload daemon to pick up new service
    systemctl daemon-reload
    systemctl enable ethos-power.service
    # Start it now to apply settings
    systemctl start ethos-power.service || echo "[!] Failed to start ethos-power.service"
    echo "[✓] Power management service configured"
fi

# Copy blacklist
if [ -f "$INSTALL_DIR/tools/ethos-power-blacklist.conf" ]; then
    cp "$INSTALL_DIR/tools/ethos-power-blacklist.conf" /etc/modprobe.d/ethos-power.conf
    echo "[✓] Peripheral blacklist configured"
fi

# ─── Final message ───
LOCAL_IP=$(hostname -I 2>/dev/null | awk '{print $1}')

echo ""
echo "═══════════════════════════════════════════════════════"
echo "  EthOS zainstalowany pomyślnie!"
echo ""
echo "  Panel:    http://${LOCAL_IP:-<IP>}:${ETHOS_PORT}"
echo "  SSH:      ssh ${ETHOS_USER}@${LOCAL_IP:-<IP>}"
echo "  Status:   sudo systemctl status ethos"
echo ""
echo "  Czas instalacji: ~$(awk '{printf "%.0f", $1/60}' /proc/uptime) min"
echo "═══════════════════════════════════════════════════════"
echo ""
echo "[✓] First boot complete at $(date)"
