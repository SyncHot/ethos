#!/bin/bash
# ─────────────────────────────────────────────────────────────
# EthOS Console Info Screen
# Synology/QNAP-style read-only status display for physical TTY.
# Runs as a systemd service — no shell, no login, no input.
# Emergency shell: Ctrl+Alt+F2 → tty2 (login required).
# ─────────────────────────────────────────────────────────────

set -u

TTY_DEV="${1:-/dev/tty1}"
ETHOS_DIR="/opt/ethos"
VERSION_FILE="$ETHOS_DIR/backend/version.json"
REFRESH=10

# Redirect output to TTY, discard all input
exec > "$TTY_DEV" 2>&1 < /dev/null

# Ignore all signals that could give shell access
trap '' INT TSTP QUIT HUP TERM

# ── Helpers ──────────────────────────────────────────────────

get_version() {
    if [ -f "$VERSION_FILE" ]; then
        grep '"version"' "$VERSION_FILE" 2>/dev/null | head -1 | \
            sed 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/'
    else
        echo "unknown"
    fi
}

get_hostname() {
    hostname 2>/dev/null || cat /etc/hostname 2>/dev/null || echo "ethos"
}

get_interfaces() {
    ip -4 addr show 2>/dev/null | \
        awk '/^[0-9]+:/ { iface=$2; gsub(/:/, "", iface) }
             /inet / && iface !~ /^lo$|^docker|^br-|^veth/ {
                 split($2, a, "/"); print iface, a[1]
             }'
}

get_uptime() {
    uptime -p 2>/dev/null | sed 's/^up //' || echo "unknown"
}

get_cpu_info() {
    local cores
    cores=$(nproc 2>/dev/null || grep -c ^processor /proc/cpuinfo 2>/dev/null || echo "?")
    local idle1 total1 idle2 total2
    read -r _ user1 nice1 sys1 idle1 iow1 irq1 sirq1 _ < /proc/stat
    total1=$((user1 + nice1 + sys1 + idle1 + iow1 + irq1 + sirq1))
    sleep 1
    read -r _ user2 nice2 sys2 idle2 iow2 irq2 sirq2 _ < /proc/stat
    total2=$((user2 + nice2 + sys2 + idle2 + iow2 + irq2 + sirq2))
    local diff_idle=$((idle2 - idle1))
    local diff_total=$((total2 - total1))
    if [ "$diff_total" -gt 0 ]; then
        local usage=$(( (diff_total - diff_idle) * 100 / diff_total ))
        echo "${usage}% (${cores} cores)"
    else
        echo "— (${cores} cores)"
    fi
}

get_memory() {
    free -h 2>/dev/null | awk '/^Mem:/ { printf "%s / %s", $3, $2 }'
}

get_disk() {
    if mountpoint -q /mnt/data 2>/dev/null; then
        df -h /mnt/data 2>/dev/null | awk 'NR==2 { printf "%s / %s (%s)", $3, $2, $5 }'
    else
        df -h / 2>/dev/null | awk 'NR==2 { printf "%s / %s (%s)", $3, $2, $5 }'
    fi
}

get_service_status() {
    if systemctl is-active ethos >/dev/null 2>&1; then
        echo "running"
    elif systemctl is-failed ethos >/dev/null 2>&1; then
        echo "failed"
    else
        echo "stopped"
    fi
}

get_temperature() {
    local temp_file raw
    for temp_file in /sys/class/thermal/thermal_zone0/temp /sys/class/hwmon/hwmon0/temp1_input; do
        if [ -f "$temp_file" ]; then
            raw=$(cat "$temp_file" 2>/dev/null)
            if [ -n "$raw" ] && [ "$raw" -gt 0 ] 2>/dev/null; then
                echo "$((raw / 1000))°C"
                return
            fi
        fi
    done
    echo "—"
}

# ── Colors ───────────────────────────────────────────────────

BOLD="\033[1m"
DIM="\033[2m"
CYAN="\033[36m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
RESET="\033[0m"

# ── Render ───────────────────────────────────────────────────

render() {
    local hn ver up cpu mem disk status temp
    hn=$(get_hostname)
    ver=$(get_version)
    up=$(get_uptime)
    cpu=$(get_cpu_info)
    mem=$(get_memory)
    disk=$(get_disk)
    status=$(get_service_status)
    temp=$(get_temperature)

    local status_color status_icon
    case "$status" in
        running) status_color="$GREEN"; status_icon="●" ;;
        failed)  status_color="$RED";   status_icon="✖" ;;
        *)       status_color="$YELLOW"; status_icon="○" ;;
    esac

    # Hide cursor, clear screen
    printf '\033[?25l'
    printf '\033[2J\033[H'

    echo ""
    echo -e "  ${CYAN}${BOLD}"
    echo "    ███████╗████████╗██╗  ██╗ ██████╗ ███████╗"
    echo "    ██╔════╝╚══██╔══╝██║  ██║██╔═══██╗██╔════╝"
    echo "    █████╗     ██║   ███████║██║   ██║███████╗"
    echo "    ██╔══╝     ██║   ██╔══██║██║   ██║╚════██║"
    echo "    ███████╗   ██║   ██║  ██║╚██████╔╝███████║"
    echo "    ╚══════╝   ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚══════╝"
    echo -e "  ${RESET}"
    echo -e "  ${DIM}NAS Operating System${RESET}                     ${DIM}v${ver}${RESET}"
    echo ""
    echo -e "  ${DIM}─────────────────────────────────────────────────${RESET}"
    echo ""

    printf "  ${BOLD}%-14s${RESET} %s\n" "Hostname:" "$hn"
    printf "  ${BOLD}%-14s${RESET} ${status_color}${status_icon} %s${RESET}\n" "EthOS:" "$status"
    printf "  ${BOLD}%-14s${RESET} %s\n" "Uptime:" "$up"
    printf "  ${BOLD}%-14s${RESET} %s\n" "Temperature:" "$temp"
    echo ""

    printf "  ${BOLD}%-14s${RESET} %s\n" "CPU:" "$cpu"
    printf "  ${BOLD}%-14s${RESET} %s\n" "Memory:" "$mem"
    printf "  ${BOLD}%-14s${RESET} %s\n" "Disk:" "$disk"
    echo ""
    echo -e "  ${DIM}─────────────────────────────────────────────────${RESET}"
    echo ""

    echo -e "  ${BOLD}Network:${RESET}"
    local has_ip=false primary_ip=""
    while IFS=' ' read -r iface ip; do
        [ -z "$iface" ] && continue
        printf "    %-12s %s\n" "${iface}:" "$ip"
        has_ip=true
        [ -z "$primary_ip" ] && primary_ip="$ip"
    done <<< "$(get_interfaces)"

    if ! $has_ip; then
        echo -e "    ${YELLOW}No network connection${RESET}"
    fi

    echo ""
    echo -e "  ${DIM}─────────────────────────────────────────────────${RESET}"
    echo ""

    if [ -n "$primary_ip" ]; then
        echo -e "  ${BOLD}Manage this device using a web browser:${RESET}"
        echo ""
        echo -e "     ${CYAN}${BOLD}→  http://${primary_ip}:9000${RESET}"
        echo ""
        echo -e "  ${BOLD}Shell access via SSH:${RESET}"
        echo ""
        echo -e "     ${DIM}→  ssh ${hn}${RESET}"
    else
        echo -e "  ${YELLOW}Connect a network cable or configure WiFi${RESET}"
        echo -e "  ${YELLOW}to access the management interface.${RESET}"
    fi

    echo ""
    echo -e "  ${DIM}─────────────────────────────────────────────────${RESET}"
    echo -e "  ${DIM}Press Ctrl+Alt+F2 for emergency shell access${RESET}"
    echo ""
}

# ── Main loop ────────────────────────────────────────────────

while true; do
    render
    sleep $REFRESH
done
