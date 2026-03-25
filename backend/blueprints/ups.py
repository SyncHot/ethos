import os
import time
import json
import threading
import subprocess
import shutil
import urllib.request
import urllib.parse
from flask import Blueprint, jsonify, request
from blueprints.eventlog import log
from utils import require_tools, check_tool

ups_bp = Blueprint('ups', __name__)

SETTINGS_FILE = '/opt/ethos/data/ups_settings.json'
NUT_CONF_DIR = '/etc/nut'

_ups_status = {
    'connected': False,
    'model': '',
    'battery_charge': 0,
    'status': 'OFF',
    'runtime': 0,
    'load': 0,
    'voltage': 0
}
_monitor_thread = None

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            pass
    return {
        'shutdown_threshold': 20,
        'shutdown_timer': 300, # 5 min on battery
        'enabled': False,
        'mode': 'usb', # usb, net
        'webhook_url': '',
        'webhook_method': 'POST'
    }

def save_settings(settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

def get_ups_name():
    try:
        r = subprocess.run(['upsc', '-l'], capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            lines = r.stdout.strip().splitlines()
            if lines:
                return lines[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return 'ups'

def parse_ups_data(output):
    data = {}
    for line in output.splitlines():
        if ':' in line:
            k, v = line.split(':', 1)
            data[k.strip()] = v.strip()
    return data

def update_status():
    global _ups_status
    name = get_ups_name()
    try:
        r = subprocess.run(['upsc', name], capture_output=True, text=True, timeout=2)
        if r.returncode == 0:
            raw = parse_ups_data(r.stdout)
            # Update in-place to preserve reference imported by app.py
            _ups_status.update({
                'connected': True,
                'model': raw.get('ups.model', 'Unknown'),
                'battery_charge': int(float(raw.get('battery.charge', 0))),
                'status': raw.get('ups.status', 'UNKNOWN'),
                'runtime': int(float(raw.get('battery.runtime', 0))),
                'load': int(float(raw.get('ups.load', 0))),
                'voltage': float(raw.get('input.voltage', 0))
            })
        else:
            _ups_status['connected'] = False
            _ups_status['status'] = 'DISCONNECTED'
    except Exception:
         _ups_status['connected'] = False
         _ups_status['status'] = 'ERROR'

def trigger_webhook(url, method, payload):
    if not url: return
    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(url, data=data, method=method.upper())
        req.add_header('Content-Type', 'application/json')
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        log('system', 'error', f'UPS Webhook failed: {e}')

def monitor_loop():
    last_status = 'OL'
    on_battery_start = 0
    
    while True:
        settings = load_settings()
        if not settings.get('enabled'):
            time.sleep(10)
            continue
            
        update_status()
        
        status = _ups_status.get('status', 'UNKNOWN')
        charge = _ups_status.get('battery_charge', 100)
        
        # Event logging
        if status != last_status:
            webhook_payload = {
                'event': 'status_change',
                'status': status,
                'charge': charge,
                'ts': time.time()
            }
            
            if 'OB' in status and 'OL' in last_status:
                log('system', 'warning', 'UPS: Switched to battery power!', {'charge': charge})
                on_battery_start = time.time()
                trigger_webhook(settings.get('webhook_url'), settings.get('webhook_method', 'POST'), webhook_payload)
            elif 'OL' in status and 'OB' in last_status:
                log('system', 'info', 'UPS: AC power restored', {'charge': charge})
                on_battery_start = 0
                trigger_webhook(settings.get('webhook_url'), settings.get('webhook_method', 'POST'), webhook_payload)
            
            last_status = status

        # Shutdown logic
        if 'OB' in status: # On Battery
            # Check threshold
            if charge < int(settings.get('shutdown_threshold', 20)):
                log('system', 'warning', f'UPS: Critical battery ({charge}%), shutting down...', {'charge': charge})
                subprocess.run(['shutdown', '-h', 'now'])
            
            # Check timer
            limit = int(settings.get('shutdown_timer', 0))
            if limit > 0 and on_battery_start > 0:
                elapsed = time.time() - on_battery_start
                if elapsed > limit:
                     log('system', 'warning', f'UPS: Battery time limit ({limit}s) reached, shutting down...', {'elapsed': elapsed})
                     subprocess.run(['shutdown', '-h', 'now'])

        time.sleep(5)

def init_ups():
    global _monitor_thread
    if _monitor_thread is None:
        _monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        _monitor_thread.start()

@ups_bp.route('/api/ups/status')
def api_status():
    err = require_tools('upsc')
    if err:
        return err
    return jsonify(_ups_status)

@ups_bp.route('/api/ups/settings', methods=['GET'])
def api_get_settings():
    return jsonify(load_settings())

@ups_bp.route('/api/ups/settings', methods=['POST'])
def api_save_settings():
    data = request.json or {}
    settings = load_settings()
    settings.update(data)
    save_settings(settings)
    
    # Reconfigure NUT if requested (simplified)
    # Ideally we should generate ups.conf here
    
    return jsonify({'ok': True})

@ups_bp.route('/api/ups/scan', methods=['POST'])
def api_scan():
    err = require_tools('upsc')
    if err:
        return err
    try:
        # Try nut-scanner
        r = subprocess.run(['nut-scanner', '-U', '-q'], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return jsonify({'found': True, 'config': r.stdout.strip()})
    except (OSError, subprocess.SubprocessError):
        pass
    return jsonify({'found': False})

@ups_bp.route('/api/ups/apply', methods=['POST'])
def api_apply_config():
    err = require_tools('upsc')
    if err:
        return err
    data = request.json or {}
    driver_config = data.get('config')
    
    if not driver_config:
        return jsonify({'error': 'No config provided'}), 400
        
    # Write to ups.conf
    try:
        conf_content = "pollinterval = 1\nmaxretry = 3\n\n[ups]\n" + driver_config + "\ndesc = EthOS Auto Configured UPS\n"
        subprocess.run(['sudo', 'tee', f'{NUT_CONF_DIR}/ups.conf'], input=conf_content, text=True, check=True)
        
        # Enable NET server mode if needed
        upsd_content = "LISTEN 0.0.0.0 3493\nLISTEN ::0 3493\n"
        subprocess.run(['sudo', 'tee', f'{NUT_CONF_DIR}/upsd.conf'], input=upsd_content, text=True, check=True)

        # Restart NUT
        subprocess.run(['sudo', 'systemctl', 'restart', 'nut-server'], timeout=10)
        subprocess.run(['sudo', 'systemctl', 'restart', 'nut-monitor'], timeout=10)
        
        # Update settings to enabled
        s = load_settings()
        s['enabled'] = True
        save_settings(s)
        
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

