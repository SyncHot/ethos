import os
import re
import json
import shlex
import shutil
import time
from flask import Blueprint, jsonify, request
from blueprints.eventlog import log

from host import host_run

# This blueprint handles WOL, Schedule, HDD Spindown, CPU Governor

power_bp = Blueprint('power', __name__)

CONFIG_FILE = "/opt/ethos/data/power_config.json"
CRON_FILE = "/etc/cron.d/ethos-power"

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        "wol_enabled": False,
        "schedule": [], # List of {"day": 0-6, "shutdown_time": "23:00", "wakeup_time": "07:00", "enabled": True}
        "hdd_spindown": {}, # {"sda": 242, "sdb": 0}
        "cpu_governor": "ondemand"
    }

def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

def run_cmd(cmd):
    r = host_run(cmd, timeout=5)
    return r.stdout.strip() if r.returncode >= 0 else ''

@power_bp.route('/status', methods=['GET'])
def get_status():
    config = load_config()

    # 1. WOL
    iface = _get_primary_iface()
    wol_status = "Unknown"
    if iface:
        out = run_cmd(f"ethtool {shlex.quote(iface)} | grep 'Wake-on'")
        # Supports Wake-on: pumbg
        # Wake-on: g
        if "Wake-on: g" in out:
            wol_status = "Enabled"
        elif "Wake-on: d" in out:
            wol_status = "Disabled"

    # 2. CPU Governor
    gov = run_cmd("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null") or "unknown"
    avail_govs = run_cmd("cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors 2>/dev/null").split()

    # 3. HDD Spindown (read from config + check actual status if possible)
    # Checking actual status (active/standby) takes time and might spin up disk, so we just show config.

    return jsonify({
        "wol": {"interface": iface, "status": wol_status, "enabled": config.get("wol_enabled", False)},
        "schedule": config.get("schedule", []),
        "hdd": config.get("hdd_spindown", {}),
        "cpu": {"current": gov, "available": avail_govs, "target": config.get("cpu_governor", "ondemand")}
    })

@power_bp.route('/save', methods=['POST'])
def save_settings():
    data = request.json or {}
    config = load_config()

    # Update Config
    if 'wol_enabled' in data:
        if not isinstance(data['wol_enabled'], bool):
            return jsonify({"error": "Invalid wol_enabled value"}), 400
        config['wol_enabled'] = data['wol_enabled']
        _apply_wol(config['wol_enabled'])

    if 'schedule' in data:
        if not isinstance(data['schedule'], list):
            return jsonify({"error": "Invalid schedule value"}), 400
        config['schedule'] = data['schedule']
        _apply_schedule(config['schedule'])

    if 'hdd_spindown' in data:
        if not isinstance(data['hdd_spindown'], dict):
            return jsonify({"error": "Invalid hdd_spindown value"}), 400
        config['hdd_spindown'] = data['hdd_spindown']
        _apply_hdd_spindown(config['hdd_spindown'])

    if 'cpu_governor' in data:
        gov = data['cpu_governor']
        allowed_govs = ('performance', 'powersave', 'ondemand', 'conservative', 'schedutil', 'userspace')
        if gov not in allowed_govs:
            return jsonify({"error": f"Invalid governor. Allowed: {', '.join(allowed_govs)}"}), 400
        config['cpu_governor'] = gov
        _apply_cpu_governor(gov)
        
    save_config(config)
    log('power', 'info', 'Power settings updated')
    return jsonify({"status": "ok"})

def _get_primary_iface():
    # Helper to find main interface (simplified)
    # ip route | grep default | awk '{print $5}'
    return run_cmd("ip route show default | awk '/default/ {print $5}' | head -n1")

def _apply_wol(enabled):
    iface = _get_primary_iface()
    if not iface or not re.match(r'^[a-zA-Z0-9_-]+$', iface):
        return
    val = 'g' if enabled else 'd'
    run_cmd(f"ethtool -s {shlex.quote(iface)} wol {val}")
    nm_con = run_cmd(f"nmcli -g GENERAL.CONNECTION dev show {shlex.quote(iface)} 2>/dev/null")
    if nm_con and re.match(r'^[\w\s._-]+$', nm_con):
        nm_val = 'magic' if enabled else 'default'
        run_cmd(f"nmcli con modify {shlex.quote(nm_con)} 802-3-ethernet.wake-on-lan {nm_val}")
    else:
        # Persist via systemd-networkd .link file
        link_dir = "/etc/systemd/network"
        os.makedirs(link_dir, exist_ok=True)
        link_file = f"{link_dir}/10-ethos-wol.link"
        wol_val = "magic" if enabled else "off"
        with open(link_file, 'w') as f:
            f.write(f"[Match]\nOriginalName={iface}\n\n[Link]\nWakeOnLan={wol_val}\n")

def _apply_schedule(schedule):
    # schedule: list of rules
    # Convert to cron lines.
    # "0 23 * * 1-5 root /sbin/shutdown -h now"
    # But we need to set wakeup time BEFORE shutdown.
    # Wrapper script: /usr/local/bin/ethos-scheduled-shutdown <wakeup_timestamp>
    
    # Actually, simpler: cron job runs python script to calculate next wakeup and set rtcwake.
    lines = []
    for rule in schedule:
        if not rule.get('enabled'): continue
        # rule: {days: [1,2,3,4,5], shutdown: "23:00", wakeup: "07:00"}
        # cron format for days: 1,2,3,4,5
        days = ",".join(map(str, rule.get('days', [])))
        if not days: continue
        
        sh = rule['shutdown'].split(':')
        
        # We need a script that takes the wakeup time for "tomorrow" (or next occurrence)
        # Construct command: /opt/ethos/tools/scheduled_shutdown.py --wakeup "07:00"
        cmd = f"/opt/ethos/tools/scheduled_shutdown.py --wakeup '{rule['wakeup']}'"
        lines.append(f"{sh[1]} {sh[0]} * * {days} root {cmd}\n")
        
    with open(CRON_FILE, 'w') as f:
        f.writelines(lines)
    # Reload cron? usually unnecessary for cron.d

def _apply_hdd_spindown(hdd_config):
    for drive, val in hdd_config.items():
        if not re.match(r'^[a-z]+$', drive):
            continue
        try:
            val = int(val)
        except (ValueError, TypeError):
            continue
        if val < 0 or val > 255:
            continue
        run_cmd(f"hdparm -S {val} /dev/{drive}")
        
    # Update udev rule for persistence
    rule_file = "/etc/udev/rules.d/99-ethos-power-custom.rules"
    rules = []
    for drive, raw_val in hdd_config.items():
        if not re.match(r'^[a-z]+$', drive):
            continue
        try:
            v = int(raw_val)
        except (ValueError, TypeError):
            continue
        if v <= 0 or v > 255:
            continue
        rules.append(f'ACTION=="add|change", KERNEL=="{drive}", RUN+="/sbin/hdparm -S {v} /dev/%k"\n')

    with open(rule_file, 'w') as f:
        f.writelines(rules)
    run_cmd("udevadm control --reload-rules")

def _apply_cpu_governor(gov):
    allowed = ('performance', 'powersave', 'ondemand', 'conservative', 'schedutil', 'userspace')
    if gov not in allowed:
        return
    run_cmd(f"cpufreq-set -r -g {gov}")
    try:
        with open("/etc/default/cpufrequtils", "w") as f:
            f.write(f'GOVERNOR="{gov}"\n')
        run_cmd("systemctl restart cpufrequtils")
    except (OSError, subprocess.SubprocessError):
        pass


# ---------------------------------------------------------------------------
# Thermal & Fan Control
# ---------------------------------------------------------------------------

THERMAL_CONFIG_FILE = "/opt/ethos/data/thermal_config.json"

PREDEFINED_CURVES = {
    'quiet':       [{'temp': 30, 'pwm_pct': 20}, {'temp': 50, 'pwm_pct': 30},
                    {'temp': 65, 'pwm_pct': 50}, {'temp': 75, 'pwm_pct': 70},
                    {'temp': 85, 'pwm_pct': 100}],
    'balanced':    [{'temp': 30, 'pwm_pct': 25}, {'temp': 45, 'pwm_pct': 40},
                    {'temp': 55, 'pwm_pct': 60}, {'temp': 70, 'pwm_pct': 80},
                    {'temp': 80, 'pwm_pct': 100}],
    'performance': [{'temp': 30, 'pwm_pct': 40}, {'temp': 40, 'pwm_pct': 60},
                    {'temp': 50, 'pwm_pct': 80}, {'temp': 60, 'pwm_pct': 100}],
}


def _load_thermal_config():
    if os.path.exists(THERMAL_CONFIG_FILE):
        try:
            with open(THERMAL_CONFIG_FILE, 'r') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        'policy': 'balanced',
        'emergency_threshold': 95,
        'custom_curve': []
    }


def _save_thermal_config(config):
    os.makedirs(os.path.dirname(THERMAL_CONFIG_FILE), exist_ok=True)
    with open(THERMAL_CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)


def _apply_fan_curve(temp_c, curve):
    """Return PWM value (0-255) using linear interpolation between curve points."""
    if not curve:
        return 0
    curve = sorted(curve, key=lambda p: p['temp'])
    if temp_c <= curve[0]['temp']:
        return int(curve[0]['pwm_pct'] * 255 / 100)
    if temp_c >= curve[-1]['temp']:
        return int(curve[-1]['pwm_pct'] * 255 / 100)
    for i in range(len(curve) - 1):
        t0, p0 = curve[i]['temp'], curve[i]['pwm_pct']
        t1, p1 = curve[i + 1]['temp'], curve[i + 1]['pwm_pct']
        if t0 <= temp_c <= t1:
            ratio = (temp_c - t0) / (t1 - t0) if t1 != t0 else 0
            pct = p0 + ratio * (p1 - p0)
            return int(pct * 255 / 100)
    return int(curve[-1]['pwm_pct'] * 255 / 100)


def _read_sysfs(path):
    """Read a sysfs file, return stripped content or empty string."""
    try:
        with open(path, 'r') as f:
            return f.read().strip()
    except (OSError, IOError):
        return ''


def _read_thermal_zones():
    """Read all thermal zones from /sys/class/thermal/thermal_zone*/."""
    zones = []
    thermal_base = '/sys/class/thermal'
    if not os.path.isdir(thermal_base):
        return zones
    for entry in sorted(os.listdir(thermal_base)):
        if not entry.startswith('thermal_zone'):
            continue
        zone_path = os.path.join(thermal_base, entry)
        zone_type = _read_sysfs(os.path.join(zone_path, 'type'))
        temp_raw = _read_sysfs(os.path.join(zone_path, 'temp'))
        try:
            temp_c = int(temp_raw) / 1000.0
        except (ValueError, TypeError):
            temp_c = None
        trip_points = []
        i = 0
        while True:
            tp_path = os.path.join(zone_path, f'trip_point_{i}_temp')
            if not os.path.exists(tp_path):
                break
            tp_raw = _read_sysfs(tp_path)
            tp_type = _read_sysfs(os.path.join(zone_path, f'trip_point_{i}_type'))
            try:
                tp_temp = int(tp_raw) / 1000.0
            except (ValueError, TypeError):
                tp_temp = None
            trip_points.append({'temp_c': tp_temp, 'type': tp_type})
            i += 1
        zones.append({
            'name': entry,
            'type': zone_type,
            'temp_c': temp_c,
            'trip_points': trip_points
        })
    return zones


def _read_fans():
    """Read fan info from /sys/class/hwmon/hwmon*/."""
    fans = []
    hwmon_base = '/sys/class/hwmon'
    if not os.path.isdir(hwmon_base):
        return fans
    for hwmon in sorted(os.listdir(hwmon_base)):
        hwmon_path = os.path.join(hwmon_base, hwmon)
        if not os.path.isdir(hwmon_path):
            continue
        fan_indices = set()
        for fname in os.listdir(hwmon_path):
            m = re.match(r'^fan(\d+)_input$', fname)
            if m:
                fan_indices.add(int(m.group(1)))
        for idx in sorted(fan_indices):
            rpm_raw = _read_sysfs(os.path.join(hwmon_path, f'fan{idx}_input'))
            label = _read_sysfs(os.path.join(hwmon_path, f'fan{idx}_label'))
            pwm_raw = _read_sysfs(os.path.join(hwmon_path, f'pwm{idx}'))
            pwm_enable = _read_sysfs(os.path.join(hwmon_path, f'pwm{idx}_enable'))
            try:
                rpm = int(rpm_raw)
            except (ValueError, TypeError):
                rpm = None
            try:
                pwm = int(pwm_raw)
            except (ValueError, TypeError):
                pwm = None
            try:
                enable = int(pwm_enable)
            except (ValueError, TypeError):
                enable = None
            mode_map = {0: 'disabled', 1: 'manual', 2: 'auto'}
            fans.append({
                'name': label or f'{hwmon}/fan{idx}',
                'rpm': rpm,
                'pwm': pwm,
                'pwm_pct': round(pwm * 100 / 255, 1) if pwm is not None else None,
                'mode': mode_map.get(enable, 'unknown'),
                'hwmon': hwmon,
                'index': idx
            })
    return fans


def _read_sensors_fallback():
    """Use lm-sensors JSON output as fallback."""
    raw = run_cmd('sensors -j 2>/dev/null')
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def _get_max_temp(zones, sensors_data):
    """Get the highest current temperature from zones or sensors."""
    temps = [z['temp_c'] for z in zones if z['temp_c'] is not None]
    if sensors_data and isinstance(sensors_data, dict):
        for chip in sensors_data.values():
            if not isinstance(chip, dict):
                continue
            for feature in chip.values():
                if not isinstance(feature, dict):
                    continue
                for key, val in feature.items():
                    if 'input' in key:
                        try:
                            temps.append(float(val))
                        except (ValueError, TypeError):
                            pass
    return max(temps) if temps else None


@power_bp.route('/thermal', methods=['GET'])
def get_thermal():
    """Get all thermal zones, fan RPMs, and current policy."""
    zones = _read_thermal_zones()
    fans = _read_fans()
    sensors_data = _read_sensors_fallback()
    config = _load_thermal_config()

    # Enrich from lm-sensors if sysfs returned no zones
    if sensors_data and not zones:
        for chip_name, chip_data in sensors_data.items():
            if not isinstance(chip_data, dict):
                continue
            for feature_name, feature_data in chip_data.items():
                if not isinstance(feature_data, dict):
                    continue
                for key, val in feature_data.items():
                    if 'input' in key and 'temp' in feature_name.lower():
                        try:
                            zones.append({
                                'name': feature_name,
                                'type': chip_name,
                                'temp_c': float(val),
                                'trip_points': []
                            })
                        except (ValueError, TypeError):
                            pass

    return jsonify({
        'zones': zones,
        'fans': fans,
        'policy': config.get('policy', 'balanced'),
        'emergency_threshold': config.get('emergency_threshold', 95),
        'custom_curve': config.get('custom_curve', [])
    })


@power_bp.route('/thermal/policy', methods=['PUT'])
def set_thermal_policy():
    """Set fan control policy.
    Body: {policy: 'quiet'|'balanced'|'performance'|'custom',
           custom_curve?: [{temp, pwm_pct}, ...]}
    """
    data = request.json or {}
    policy = data.get('policy')
    if policy not in ('quiet', 'balanced', 'performance', 'custom'):
        return jsonify({'error': 'Invalid policy. Must be quiet, balanced, performance, or custom'}), 400

    config = _load_thermal_config()
    config['policy'] = policy

    if policy == 'custom':
        custom_curve = data.get('custom_curve')
        if not isinstance(custom_curve, list) or len(custom_curve) < 2:
            return jsonify({'error': 'custom_curve must be a list of at least 2 {temp, pwm_pct} points'}), 400
        for point in custom_curve:
            if not isinstance(point, dict) or 'temp' not in point or 'pwm_pct' not in point:
                return jsonify({'error': 'Each curve point must have temp and pwm_pct'}), 400
            try:
                t = int(point['temp'])
                p = int(point['pwm_pct'])
            except (ValueError, TypeError):
                return jsonify({'error': 'temp and pwm_pct must be integers'}), 400
            if not (0 <= t <= 120):
                return jsonify({'error': 'temp must be between 0 and 120'}), 400
            if not (0 <= p <= 100):
                return jsonify({'error': 'pwm_pct must be between 0 and 100'}), 400
        config['custom_curve'] = [{'temp': int(p['temp']), 'pwm_pct': int(p['pwm_pct'])} for p in custom_curve]
        curve = config['custom_curve']
    else:
        curve = PREDEFINED_CURVES[policy]

    _save_thermal_config(config)

    # Apply fan curve to all hwmon PWM controls
    zones = _read_thermal_zones()
    sensors_data = _read_sensors_fallback()
    current_temp = _get_max_temp(zones, sensors_data)

    if current_temp is not None:
        pwm_value = _apply_fan_curve(current_temp, curve)
        hwmon_base = '/sys/class/hwmon'
        if os.path.isdir(hwmon_base):
            for hwmon in sorted(os.listdir(hwmon_base)):
                hwmon_path = os.path.join(hwmon_base, hwmon)
                if not os.path.isdir(hwmon_path):
                    continue
                for fname in os.listdir(hwmon_path):
                    m = re.match(r'^pwm(\d+)$', fname)
                    if not m:
                        continue
                    idx = m.group(1)
                    enable_path = os.path.join(hwmon_path, f'pwm{idx}_enable')
                    pwm_path = os.path.join(hwmon_path, f'pwm{idx}')
                    host_run(f"bash -c 'echo 1 > {shlex.quote(enable_path)}'", timeout=5)
                    host_run(f"bash -c 'echo {pwm_value} > {shlex.quote(pwm_path)}'", timeout=5)

    log('power', 'info', f'Thermal policy set to {policy}')
    return jsonify({'ok': True, 'policy': policy})


@power_bp.route('/thermal/emergency', methods=['PUT'])
def set_emergency_threshold():
    """Set emergency shutdown temperature.
    Body: {threshold_celsius: int}
    """
    data = request.json or {}
    threshold = data.get('threshold_celsius')

    if threshold is None:
        return jsonify({'error': 'threshold_celsius is required'}), 400
    try:
        threshold = int(threshold)
    except (ValueError, TypeError):
        return jsonify({'error': 'threshold_celsius must be an integer'}), 400
    if not (50 <= threshold <= 120):
        return jsonify({'error': 'threshold_celsius must be between 50 and 120'}), 400

    config = _load_thermal_config()
    config['emergency_threshold'] = threshold
    _save_thermal_config(config)

    log('power', 'info', f'Emergency thermal threshold set to {threshold}\u00b0C')
    return jsonify({'ok': True, 'emergency_threshold': threshold})
