#!/bin/bash
# Instalator usługi systemd dla EthOS Ticket Watcher
# Uruchom: sudo bash /opt/ethos/tools/install_ticket_watcher_service.sh

set -e

SERVICE_FILE="/opt/ethos/tools/ethos-ticket-watcher.service"
SYSTEMD_PATH="/etc/systemd/system/ethos-ticket-watcher.service"
VENV_PIP="/opt/ethos/venv/bin/pip"
REQUIREMENTS="/opt/ethos/backend/requirements.txt"

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: Ten skrypt wymaga uprawnień root (sudo)." >&2
    exit 1
fi

echo "→ Instalowanie zależności Python w venv..."
if [[ -x "$VENV_PIP" && -f "$REQUIREMENTS" ]]; then
    "$VENV_PIP" install -r "$REQUIREMENTS" --quiet
    echo "  ✓ Zależności zainstalowane z $REQUIREMENTS"
else
    echo "  ⚠️ Pominięto: nie znaleziono $VENV_PIP lub $REQUIREMENTS"
fi

echo "→ Kopiowanie pliku jednostki..."
cp "$SERVICE_FILE" "$SYSTEMD_PATH"
chmod 644 "$SYSTEMD_PATH"

echo "→ Przeładowanie konfiguracji systemd..."
systemctl daemon-reload

echo "→ Włączanie automatycznego startu..."
systemctl enable ethos-ticket-watcher

echo "→ Uruchamianie usługi..."
systemctl start ethos-ticket-watcher

echo ""
echo "✅ Usługa ethos-ticket-watcher zainstalowana i uruchomiona."
echo ""
echo "Zarządzanie:"
echo "  sudo systemctl status  ethos-ticket-watcher"
echo "  sudo systemctl start   ethos-ticket-watcher"
echo "  sudo systemctl stop    ethos-ticket-watcher"
echo "  sudo systemctl restart ethos-ticket-watcher"
echo "  sudo systemctl enable  ethos-ticket-watcher   # autostart przy boot"
echo "  sudo systemctl disable ethos-ticket-watcher   # wyłącz autostart"
echo ""
echo "Logi:"
echo "  tail -f /opt/ethos/logs/ticket_watcher.log"
echo "  journalctl -u ethos-ticket-watcher -f"
