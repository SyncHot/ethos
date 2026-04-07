#!/usr/bin/env bash
# ============================================================
#  Video Station — GMKtec NucBox G3 (Intel N100/N95) Monitor
#  Monitoruje: CPU, RAM, GPU (VAAPI/i915), temperaturę, FFmpeg
#  Użycie:
#    ./monitor_nuc.sh                    # monitor live (Ctrl+C aby zakończyć)
#    ./monitor_nuc.sh --log session.csv  # zapis do CSV
#    ./monitor_nuc.sh --duration 1800    # 30 minut, potem exit
# ============================================================
set -euo pipefail

LOG_FILE=""
DURATION=0          # 0 = infinite
INTERVAL=5          # sekundy między pomiarami
SESSION_URL="http://localhost:9000/api/video-station"
VS_TOKEN=""

# ── parse args ──────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case $1 in
        --log)        LOG_FILE="$2";   shift 2 ;;
        --duration)   DURATION="$2";  shift 2 ;;
        --interval)   INTERVAL="$2";  shift 2 ;;
        --token)      VS_TOKEN="$2";  shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# ── detect GPU render node ───────────────────────────────────
RENDER_NODE="/dev/dri/renderD128"
HAS_INTEL_GPU=false
HAS_INTEL_GPU_TOP=false
if [[ -e "$RENDER_NODE" ]]; then
    HAS_INTEL_GPU=true
    command -v intel_gpu_top &>/dev/null && HAS_INTEL_GPU_TOP=true
fi

# ── detect temp sensors ──────────────────────────────────────
THERMAL_ZONE=""
for z in /sys/class/thermal/thermal_zone*/; do
    type_file="${z}type"
    if [[ -f "$type_file" ]]; then
        t=$(cat "$type_file")
        if [[ "$t" == "x86_pkg_temp" ]] || [[ "$t" == "coretemp" ]]; then
            THERMAL_ZONE="${z}temp"
            break
        fi
    fi
done
# fallback: first zone with temp
if [[ -z "$THERMAL_ZONE" ]]; then
    THERMAL_ZONE=$(ls /sys/class/thermal/thermal_zone*/temp 2>/dev/null | head -1)
fi

# ── CSV header ───────────────────────────────────────────────
CSV_HEADER="timestamp,cpu_pct,ram_pct,ram_used_mb,temp_c,ffmpeg_procs,gpu_video_pct,gpu_render_pct,vs_sessions,seek_latency_ms"
if [[ -n "$LOG_FILE" ]]; then
    echo "$CSV_HEADER" > "$LOG_FILE"
    echo "📄 Logging to: $LOG_FILE"
fi

# ── helpers ──────────────────────────────────────────────────
get_cpu() {
    # One-shot CPU reading via /proc/stat (2-sample diff)
    local s1 s2 idle1 idle2 total1 total2 idle_d total_d
    read -r _ s1 < /proc/stat
    IFS=' ' read -ra f1 <<< "$s1"
    sleep 0.5
    read -r _ s2 < /proc/stat
    IFS=' ' read -ra f2 <<< "$s2"
    idle1=${f1[3]}; idle2=${f2[3]}
    total1=0; total2=0
    for v in "${f1[@]}"; do total1=$((total1+v)); done
    for v in "${f2[@]}"; do total2=$((total2+v)); done
    idle_d=$((idle2 - idle1))
    total_d=$((total2 - total1))
    if [[ $total_d -gt 0 ]]; then
        echo $(( 100 * (total_d - idle_d) / total_d ))
    else
        echo 0
    fi
}

get_ram() {
    local total free buffers cached avail used_mb pct
    total=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
    avail=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
    used_mb=$(( (total - avail) / 1024 ))
    pct=$(( 100 * (total - avail) / total ))
    echo "$pct $used_mb"
}

get_temp() {
    if [[ -n "$THERMAL_ZONE" && -f "$THERMAL_ZONE" ]]; then
        local raw
        raw=$(cat "$THERMAL_ZONE")
        echo $(( raw / 1000 ))
    else
        echo "N/A"
    fi
}

get_ffmpeg_procs() {
    pgrep -c ffmpeg 2>/dev/null || echo 0
}

get_vs_sessions() {
    if [[ -n "$VS_TOKEN" ]]; then
        curl -sf -H "Authorization: Bearer $VS_TOKEN" \
            "${SESSION_URL}/hls/sessions" 2>/dev/null | \
            python3 -c "import sys,json; d=json.load(sys.stdin); print(len(d.get('sessions',{})))" 2>/dev/null || echo "?"
    else
        echo "?"
    fi
}

gpu_video_pct=0
gpu_render_pct=0
get_gpu_intel() {
    # intel_gpu_top outputs JSON with -J flag (if available)
    if $HAS_INTEL_GPU_TOP; then
        local raw
        raw=$(timeout 3 intel_gpu_top -J -s 1 2>/dev/null | tail -1) || true
        if [[ -n "$raw" ]]; then
            gpu_video_pct=$(echo "$raw" | python3 -c \
                "import sys,json; d=json.load(sys.stdin); print(int(d.get('engines',{}).get('Video/0',{}).get('busy',0)))" 2>/dev/null || echo 0)
            gpu_render_pct=$(echo "$raw" | python3 -c \
                "import sys,json; d=json.load(sys.stdin); print(int(d.get('engines',{}).get('Render/3D/0',{}).get('busy',0)))" 2>/dev/null || echo 0)
        fi
    else
        # fallback: read i915 PMU counters via /sys if available
        gpu_video_pct="N/A"
        gpu_render_pct="N/A"
    fi
}

# ── thermal throttle warning ─────────────────────────────────
check_throttle() {
    local temp=$1
    if [[ "$temp" != "N/A" ]] && [[ $temp -ge 90 ]]; then
        echo "⚠️  THERMAL THROTTLE WARNING: ${temp}°C"
    elif [[ "$temp" != "N/A" ]] && [[ $temp -ge 80 ]]; then
        echo "🌡️  High temp: ${temp}°C — approaching throttle threshold"
    fi
}

# ── banner ───────────────────────────────────────────────────
clear
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   Video Station — NucBox G3 (N100/N95) Resource Monitor     ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Intel VAAPI:  $(if $HAS_INTEL_GPU; then echo "✅ $RENDER_NODE"; else echo "❌ Not found"; fi)"
echo "║  intel_gpu_top: $(if $HAS_INTEL_GPU_TOP; then echo "✅ available"; else echo "⚠️  not installed (apt install intel-gpu-tools)"; fi)"
echo "║  Thermal zone: ${THERMAL_ZONE:-"⚠️  not found"}"
if [[ -n "$LOG_FILE" ]]; then
echo "║  CSV log:      $LOG_FILE"
fi
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── main loop ────────────────────────────────────────────────
start_time=$(date +%s)
declare -a cpu_samples=()
declare -a temp_samples=()
throttle_events=0
peak_cpu=0
peak_temp=0

printf "%-20s %6s %6s %8s %6s %8s %10s %12s\n" \
    "TIME" "CPU%" "RAM%" "RAM(MB)" "°C" "FFMPEG" "GPU-Video%" "GPU-Render%"
printf "%-20s %6s %6s %8s %6s %8s %10s %12s\n" \
    "--------------------" "------" "------" "--------" "------" "--------" "----------" "------------"

while true; do
    ts=$(date +"%Y-%m-%d %H:%M:%S")
    cpu=$(get_cpu)
    read -r ram_pct ram_mb <<< "$(get_ram)"
    temp=$(get_temp)
    ffmpeg_n=$(get_ffmpeg_procs)
    vs_sess=$(get_vs_sessions)
    get_gpu_intel  # updates gpu_video_pct, gpu_render_pct

    # track peaks
    cpu_samples+=("$cpu")
    [[ $cpu -gt $peak_cpu ]] && peak_cpu=$cpu
    if [[ "$temp" != "N/A" ]]; then
        temp_samples+=("$temp")
        [[ $temp -gt $peak_temp ]] && peak_temp=$temp
        [[ $temp -ge 90 ]] && throttle_events=$((throttle_events+1))
    fi

    # print row
    printf "%-20s %5d%% %5d%% %8d %5s°C %8s %9s%% %11s%%\n" \
        "$ts" "$cpu" "$ram_pct" "$ram_mb" "$temp" "$ffmpeg_n" \
        "$gpu_video_pct" "$gpu_render_pct"

    # throttle warning
    check_throttle "$temp"

    # CSV append
    if [[ -n "$LOG_FILE" ]]; then
        echo "${ts},${cpu},${ram_pct},${ram_mb},${temp},${ffmpeg_n},${gpu_video_pct},${gpu_render_pct},${vs_sess},0" >> "$LOG_FILE"
    fi

    # duration check
    if [[ $DURATION -gt 0 ]]; then
        now=$(date +%s)
        elapsed=$((now - start_time))
        if [[ $elapsed -ge $DURATION ]]; then
            break
        fi
    fi

    sleep "$INTERVAL"
done

# ── summary ──────────────────────────────────────────────────
echo ""
echo "══════════════════════════════════════════"
echo " Session Summary"
echo "══════════════════════════════════════════"
echo " Peak CPU:       ${peak_cpu}%"
echo " Peak Temp:      ${peak_temp}°C"
echo " Throttle events (≥90°C): ${throttle_events}"
if $HAS_INTEL_GPU_TOP; then
    echo " GPU Video peak: check CSV for trend"
fi
echo " Samples:        ${#cpu_samples[@]}"
if [[ ${#cpu_samples[@]} -gt 0 ]]; then
    total_cpu=0
    for s in "${cpu_samples[@]}"; do total_cpu=$((total_cpu+s)); done
    echo " Avg CPU:        $((total_cpu / ${#cpu_samples[@]}))%"
fi
if [[ -n "$LOG_FILE" ]]; then
    echo " CSV saved:      $LOG_FILE"
fi
echo "══════════════════════════════════════════"
