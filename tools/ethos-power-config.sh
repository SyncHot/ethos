#!/bin/bash
# EthOS Power Management Configuration
# Applied on boot via ethos-power.service

set -u

# 1. CPU Governor
# Prefer 'schedutil' if available, otherwise 'powersave'
AVAILABLE_GOVERNORS=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null || echo "")
TARGET_GOVERNOR="powersave"

if [[ "$AVAILABLE_GOVERNORS" == *"schedutil"* ]]; then
    TARGET_GOVERNOR="schedutil"
fi

if [ -d /sys/devices/system/cpu/cpu0/cpufreq ]; then
    echo "[Power] Setting CPU governor to $TARGET_GOVERNOR..."
    # Apply to all cores
    for cpu_gov in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
        if [ -f "$cpu_gov" ]; then
             echo "$TARGET_GOVERNOR" > "$cpu_gov" 2>/dev/null || true
        fi
    done
else
    echo "[Power] CPU frequency scaling not available."
fi

# 2. Disk Spindown (Rotational drives only)
# 242 = 1 hour (values 1-240 are 5s steps, 241-251 are 30min steps)
SPINDOWN_TIME=242 

echo "[Power] Configuring disk spindown..."
for disk in /sys/block/sd*; do
    # Ensure glob matched
    [ -e "$disk" ] || continue
    
    if [ -d "$disk" ]; then
        # Check if rotational (1 = rotational, 0 = ssd)
        ROTATIONAL=$(cat "$disk/queue/rotational" 2>/dev/null || echo "0")
        if [ "$ROTATIONAL" -eq "1" ]; then
            devname=$(basename "$disk")
            echo "[Power] Setting spindown for /dev/$devname (1 hour)"
            hdparm -S $SPINDOWN_TIME "/dev/$devname" > /dev/null 2>&1 || echo "[Power] Failed to set spindown for $devname"
        fi
    fi
done

# 3. Disable Peripherals (Runtime)

# Bluetooth
echo "[Power] Disabling Bluetooth..."
rfkill block bluetooth 2>/dev/null || true
# Service disabling is handled by systemd preset or installer script

# HDMI / Display
# If we are headless, we can try to turn off display outputs
# For simple framebuffer devices or when xrandr is not available, we rely on kernel params
# but we can try to disable output if possible.
# On RPi: tvservice -o (if available)
if command -v tvservice >/dev/null 2>&1; then
   tvservice -o 2>/dev/null || true
fi

# Audio
# Runtime muting or disabling is hard without removing modules.
# We rely on blacklist for modules.
