#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════
#  EthOS — WiFi Access Point Manager
#  Creates/destroys a "ethos" hotspot so users can connect
#  to RPi when no WiFi is configured, then set up real WiFi
#  from the EthOS panel.
#
#  Usage:
#    ethos-ap start   — start AP (auto if no WiFi)
#    ethos-ap stop    — stop AP, reconnect normal WiFi
#    ethos-ap status  — show whether AP is active
#    ethos-ap auto    — start AP only if no network
# ═══════════════════════════════════════════════════════════
# NOTE: Do NOT use 'set -e' here — this runs as a systemd oneshot init
# script where any non-zero exit kills the service. Individual errors are
# handled explicitly.
set -u

AP_SSID="ethos"
AP_CON_NAME="ethos-hotspot"
AP_IP="192.168.42.1"
AP_SUBNET="192.168.42.0/24"
DNSMASQ_CONF="/tmp/ethos-dnsmasq.conf"
DNSMASQ_PID="/tmp/ethos-dnsmasq.pid"
MINI_DHCP_PID="/tmp/ethos-dhcp.pid"

# Locate mini-dhcp.py (bundled fallback DHCP server)
MINI_DHCP=""
for p in /opt/ethos-installer/mini-dhcp.py /opt/ethos/installer/mini-dhcp.py \
         "$(dirname "$0")/mini-dhcp.py"; do
    [[ -f "$p" ]] && { MINI_DHCP="$p"; break; }
done

log()  { echo "[✓] $*"; }
info() { echo "[i] $*"; }
err()  { echo "[✗] $*" >&2; }

# ─── Check if dnsmasq binary is available ───
has_dnsmasq() {
    command -v dnsmasq &>/dev/null
}

# ─── Find WiFi interface ───
find_wifi_iface() {
    # Avoid pipe+while+return which causes SIGPIPE (exit 141) with pipefail.
    local output
    output=$(nmcli -t -f DEVICE,TYPE device status 2>/dev/null) || true
    local line
    while IFS=: read -r dev type; do
        if [[ "$type" == "wifi" ]]; then
            echo "$dev"
            return 0
        fi
    done <<< "$output"
    return 1
}

# ─── Check if we have any working network ───
has_network() {
    # 1) Try ping (fastest)
    local gw
    gw=$(ip route show default 2>/dev/null | awk '{print $3; exit}')
    if [[ -n "$gw" ]]; then
        ping -c 1 -W 3 "$gw" &>/dev/null && return 0
    fi
    # 2) Fallback: TCP connect (works when ICMP blocked, e.g. QEMU user-mode)
    if [[ -n "$gw" ]]; then
        timeout 3 bash -c "echo > /dev/tcp/$gw/53" 2>/dev/null && return 0
        # Try external DNS port
        timeout 3 bash -c 'echo > /dev/tcp/8.8.8.8/53' 2>/dev/null && return 0
    fi
    # 3) Check if any non-AP WiFi is connected
    local wifi_iface
    wifi_iface=$(find_wifi_iface)
    if [[ -n "$wifi_iface" ]]; then
        local conn
        conn=$(nmcli -t -f GENERAL.CONNECTION device show "$wifi_iface" 2>/dev/null | grep -oP '(?<=:).+' || true)
        if [[ -n "$conn" && "$conn" != "--" && "$conn" != "$AP_CON_NAME" ]]; then
            return 0
        fi
    fi
    # 4) Check any non-loopback interface with global IP + working route
    if ip -4 addr show scope global 2>/dev/null | grep -q 'inet ' && [[ -n "$gw" ]]; then
        return 0
    fi
    return 1
}

# ─── Check if AP is running ───
is_ap_active() {
    nmcli -t -f NAME,TYPE connection show --active 2>/dev/null | grep -q "^${AP_CON_NAME}:.*wireless" && return 0
    return 1
}

start_ap() {
    local wifi_iface=""
    # Retry finding WiFi interface (NM may need time to detect hardware)
    for try in $(seq 1 10); do
        wifi_iface=$(find_wifi_iface) || true
        if [[ -n "$wifi_iface" ]]; then
            break
        fi
        info "Szukam interfejsu WiFi... (próba $try/10)"
        sleep 3
    done
    if [[ -z "$wifi_iface" ]]; then
        err "Brak interfejsu WiFi (sprawdź czy firmware jest zainstalowany)"
        nmcli device status 2>/dev/null || true
        return 1
    fi

    info "Znaleziono WiFi: $wifi_iface"

    if is_ap_active; then
        info "Hotspot już aktywny (SSID: $AP_SSID)"
        return 0
    fi

    info "Uruchamiam otwarty hotspot WiFi: $AP_SSID (bez hasła)"

    # Unblock WiFi if blocked by rfkill
    rfkill unblock wifi 2>/dev/null || true
    sleep 1

    # Delete old AP connection if exists
    nmcli connection delete "$AP_CON_NAME" 2>/dev/null || true

    if has_dnsmasq; then
        # ── Full mode: NM shared (dnsmasq provides DHCP + DNS) ──
        info "dnsmasq dostępny — tryb shared"
        nmcli connection add \
            type wifi \
            con-name "$AP_CON_NAME" \
            ifname "$wifi_iface" \
            ssid "$AP_SSID" \
            autoconnect no \
            wifi.mode ap \
            wifi.band bg \
            wifi.channel 6 \
            ipv4.method shared \
            ipv4.addresses "${AP_IP}/24" \
            ipv6.method disabled 2>&1 || {
            err "Nie udało się utworzyć połączenia AP (shared)"
            return 1
        }
    else
        # ── Fallback: NM manual IP + Python mini-DHCP ──
        info "dnsmasq niedostępny — tryb manual + mini-dhcp.py"
        nmcli connection add \
            type wifi \
            con-name "$AP_CON_NAME" \
            ifname "$wifi_iface" \
            ssid "$AP_SSID" \
            autoconnect no \
            wifi.mode ap \
            wifi.band bg \
            wifi.channel 6 \
            ipv4.method manual \
            ipv4.addresses "${AP_IP}/24" \
            ipv6.method disabled 2>&1 || {
            err "Nie udało się utworzyć połączenia AP (manual)"
            return 1
        }
    fi

    nmcli connection up "$AP_CON_NAME" 2>&1 || {
        err "Nie udało się aktywować hotspota"
        nmcli connection delete "$AP_CON_NAME" 2>/dev/null || true
        return 1
    }

    # Wait for AP to be ready
    sleep 2

    # Start fallback DHCP if no dnsmasq
    if ! has_dnsmasq && [[ -n "$MINI_DHCP" ]]; then
        # Kill any leftover
        [[ -f "$MINI_DHCP_PID" ]] && kill "$(cat "$MINI_DHCP_PID")" 2>/dev/null || true
        python3 "$MINI_DHCP" "$wifi_iface" &
        disown
        sleep 1
        log "Mini-DHCP aktywny (Python fallback)"
    elif ! has_dnsmasq; then
        err "Brak dnsmasq i mini-dhcp.py — klienci muszą ustawić IP ręcznie!"
    fi

    if is_ap_active; then
        log "Hotspot aktywny: SSID=$AP_SSID, IP=$AP_IP"
        log "Panel EthOS: http://${AP_IP}:9000"
    else
        err "Nie udało się uruchomić hotspota"
        exit 1
    fi
}

stop_ap() {
    info "Wyłączam hotspot..."

    nmcli connection down "$AP_CON_NAME" 2>/dev/null || true
    nmcli connection delete "$AP_CON_NAME" 2>/dev/null || true

    # Kill dnsmasq if we started it
    if [[ -f "$DNSMASQ_PID" ]]; then
        kill "$(cat "$DNSMASQ_PID")" 2>/dev/null || true
        rm -f "$DNSMASQ_PID" "$DNSMASQ_CONF"
    fi

    # Kill mini-dhcp.py if running
    if [[ -f "$MINI_DHCP_PID" ]]; then
        kill "$(cat "$MINI_DHCP_PID")" 2>/dev/null || true
        rm -f "$MINI_DHCP_PID"
    fi

    # Try to reconnect to known WiFi
    local wifi_iface=""
    wifi_iface=$(find_wifi_iface) || true
    if [[ -n "$wifi_iface" ]]; then
        nmcli device set "$wifi_iface" autoconnect yes 2>/dev/null || true
        nmcli device connect "$wifi_iface" 2>/dev/null || true
    fi

    log "Hotspot wyłączony"
}

status_ap() {
    if is_ap_active; then
        local wifi_iface=""
        wifi_iface=$(find_wifi_iface) || true
        echo "active"
        echo "ssid=$AP_SSID"
        echo "interface=$wifi_iface"
        echo "ip=$AP_IP"
        # Connected clients
        local clients
        clients=$(arp -i "${wifi_iface:-wlan0}" -n 2>/dev/null | grep -cv 'incomplete\|Address' || echo "0")
        echo "clients=$clients"
    else
        echo "inactive"
    fi
}

auto_ap() {
    # Wait a bit for normal networking to come up
    info "Czekam 30s na sieć..."
    for i in $(seq 1 15); do
        if has_network; then
            info "Sieć dostępna — hotspot niepotrzebny"
            return 0
        fi
        sleep 2
    done

    # If there's a saved WiFi connection (user configured WiFi in preboot),
    # wait another 90s — WiFi association + DHCP can take time on slow adapters.
    # NOTE: This should be LONGER than firstboot's 90s wait, so firstboot has
    # a chance to detect network before AP takes over the WiFi interface.
    local saved_wifi
    saved_wifi=$(nmcli -t -f TYPE,NAME connection show 2>/dev/null \
        | grep '^802-11-wireless:' | grep -v "$AP_CON_NAME" | head -1) || true
    if [[ -n "$saved_wifi" ]]; then
        info "Znaleziono zapisane WiFi — czekam dodatkowe 90s..."
        for i in $(seq 1 45); do
            if has_network; then
                info "Sieć dostępna — hotspot niepotrzebny"
                return 0
            fi
            sleep 2
        done
    fi

    info "Brak sieci po timeout — uruchamiam hotspot"
    # Retry AP start up to 3 times (driver/firmware may need warmup)
    for attempt in 1 2 3; do
        if start_ap; then
            return 0
        fi
        err "Próba $attempt/3 nie powiodła się"
        sleep 5
    done
    err "Nie udało się uruchomić hotspota po 3 próbach"
    return 1
}

# ─── Main ───
case "${1:-}" in
    start)  start_ap ;;
    stop)   stop_ap ;;
    status) status_ap ;;
    auto)   auto_ap ;;
    *)
        echo "Użycie: $(basename "$0") {start|stop|status|auto}"
        echo ""
        echo "  start   — uruchom otwarty hotspot WiFi (SSID: $AP_SSID)"
        echo "  stop    — wyłącz hotspot, połącz z normalną siecią"
        echo "  status  — sprawdź stan hotspota"
        echo "  auto    — uruchom hotspot tylko jeśli brak sieci"
        exit 1
        ;;
esac
