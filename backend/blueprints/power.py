import os
import re
import json
import shlex
import shutil
import subprocess
import time
from flask import Blueprint, jsonify, request
from blueprints.eventlog import log

# This blueprint handles WOL, Schedule, HDD Spindown, CPU Governor

power_bp = Blueprint('power', __name__)

CONFIG_FILE = "/opt/ethos/data/power_config.json"
CRON_FILE = "/etc/cron.d/ethos-power"

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except:
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
    try:
        res = subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=5)
        return res.stdout.strip()
    except Exception as e:
        return str(e)

@power_bp.route('/status', methods=['GET'])
def get_status():
    config = load_config()

    # 1. WOL
    iface = _get_primary_iface()
    wol_status = "Unknown"
    if iface:
        out = run_cmd(f"ethtool {iface} | grep 'Wake-on'")
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
    data = request.json
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
    except:
        pass
