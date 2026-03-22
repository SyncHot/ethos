import os
import re
import json
import shutil
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
        config['wol_enabled'] = data['wol_enabled']
        _apply_wol(config['wol_enabled'])
        
    if 'schedule' in data:
        config['schedule'] = data['schedule']
        _apply_schedule(config['schedule'])
        
    if 'hdd_spindown' in data:
        config['hdd_spindown'] = data['hdd_spindown']
        _apply_hdd_spindown(config['hdd_spindown'])
        
    if 'cpu_governor' in data:
        config['cpu_governor'] = data['cpu_governor']
        _apply_cpu_governor(config['cpu_governor'])
        
    save_config(config)
    log("Power settings updated")
    return jsonify({"status": "ok"})

def _get_primary_iface():
    # Helper to find main interface (simplified)
    # ip route | grep default | awk '{print $5}'
    return run_cmd("ip route show default | awk '/default/ {print $5}' | head -n1")

def _apply_wol(enabled):
    iface = _get_primary_iface()
    if not iface: return
    val = 'g' if enabled else 'd'
    # Apply now
    run_cmd(f"ethtool -s {iface} wol {val}")
    # Persist via NetworkManager dispatcher or systemd link?
    # For now, we apply it. A reboot might reset it unless we add a persistent config.
    # On Debian/Ubuntu with systemd-networkd, it's in .link file. 
    # With NetworkManager, 'nmcli c modify <con> 802-3-ethernet.wake-on-lan magic'.
    # We'll try nmcli if available, otherwise just rely on ethtool in a startup script (rc.local equivalent)
    # Simplest persistence: add to a script ran at boot.
    # ethos-system-helper.sh runs at boot? check tools/ethos-system-helper.sh
    pass 

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
    # hdd_config: {"sda": 120, "sdb": 0}
    # Apply immediately
    for drive, val in hdd_config.items():
        if not re.match(r'^[a-z]+$', drive): continue
        run_cmd(f"hdparm -S {val} /dev/{drive}")
        
    # Update udev rule for persistence
    # We overwrite the rule file with specific rules for each drive
    rule_file = "/etc/udev/rules.d/99-ethos-power-custom.rules"
    rules = []
    for drive, val in hdd_config.items():
        if val == 0: continue
        # Match by kernel name is risky if they change, but standard for simple setups.
        # Ideally by UUID/Serial, but let's stick to simple implementation first.
        rules.append(f'ACTION=="add|change", KERNEL=="{drive}", RUN+="/sbin/hdparm -S {val} /dev/%k"\n')
        
    with open(rule_file, 'w') as f:
        f.writelines(rules)
    run_cmd("udevadm control --reload-rules")

def _apply_cpu_governor(gov):
    # Apply to all CPUs
    run_cmd(f"cpufreq-set -r -g {gov}")
    # Persist via cpufrequtils default
    try:
        with open("/etc/default/cpufrequtils", "w") as f:
            f.write(f'GOVERNOR="{gov}"\n')
        run_cmd("systemctl restart cpufrequtils")
    except:
        pass

import subprocess
