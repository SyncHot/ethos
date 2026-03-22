import os
import time
import json
import threading
import subprocess
import shutil
from flask import Blueprint, jsonify, request
from blueprints.eventlog import log

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
        except:
            pass
    return {
        'shutdown_threshold': 20,
        'shutdown_timer': 300, # 5 min on battery
        'enabled': False,
        'mode': 'usb' # usb, net
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
    except:
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
            _ups_status = {
                'connected': True,
                'model': raw.get('ups.model', 'Unknown'),
                'battery_charge': int(float(raw.get('battery.charge', 0))),
                'status': raw.get('ups.status', 'UNKNOWN'),
                'runtime': int(float(raw.get('battery.runtime', 0))),
                'load': int(float(raw.get('ups.load', 0))),
                'voltage': float(raw.get('input.voltage', 0))
            }
        else:
            _ups_status['connected'] = False
            _ups_status['status'] = 'DISCONNECTED'
    except Exception:
         _ups_status['connected'] = False
         _ups_status['status'] = 'ERROR'

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
            if 'OB' in status and 'OL' in last_status:
                log('system', 'warning', 'Zasilanie UPS: Przejście na baterię!', {'charge': charge})
                on_battery_start = time.time()
                # Notification could be sent here (log handles socketio emit)
            elif 'OL' in status and 'OB' in last_status:
                log('system', 'info', 'Zasilanie UPS: Przywrócono zasilanie sieciowe', {'charge': charge})
                on_battery_start = 0
            last_status = status

        # Shutdown logic
        if 'OB' in status: # On Battery
            # Check threshold
            if charge < int(settings.get('shutdown_threshold', 20)):
                log('system', 'warning', f'UPS: Bateria krytyczna ({charge}%), zamykanie systemu...', {'charge': charge})
                subprocess.run(['shutdown', '-h', 'now'])
            
            # Check timer
            limit = int(settings.get('shutdown_timer', 0))
            if limit > 0 and on_battery_start > 0:
                elapsed = time.time() - on_battery_start
                if elapsed > limit:
                     log('system', 'warning', f'UPS: Limit czasu na baterii ({limit}s) osiągnięty, zamykanie...', {'elapsed': elapsed})
                     subprocess.run(['shutdown', '-h', 'now'])

        time.sleep(5)

def init_ups():
    global _monitor_thread
    if _monitor_thread is None:
        _monitor_thread = threading.Thread(target=monitor_loop, daemon=True)
        _monitor_thread.start()

@ups_bp.route('/api/ups/status')
def api_status():
    return jsonify(_ups_status)

@ups_bp.route('/api/ups/settings', methods=['GET'])
def api_get_settings():
    return jsonify(load_settings())

@ups_bp.route('/api/ups/settings', methods=['POST'])
def api_save_settings():
    data = request.json
    settings = load_settings()
    settings.update(data)
    save_settings(settings)
    
    # Reconfigure NUT if requested (simplified)
    # Ideally we should generate ups.conf here
    
    return jsonify({'ok': True})

@ups_bp.route('/api/ups/scan', methods=['POST'])
def api_scan():
    try:
        # Try nut-scanner
        r = subprocess.run(['nut-scanner', '-U', '-q'], capture_output=True, text=True, timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            return jsonify({'found': True, 'config': r.stdout.strip()})
    except:
        pass
    return jsonify({'found': False})

@ups_bp.route('/api/ups/apply', methods=['POST'])
def api_apply_config():
    data = request.json
    driver_config = data.get('config')
    
    if not driver_config:
        return jsonify({'error': 'No config provided'}), 400
        
    # Write to ups.conf
    try:
        with open(f'{NUT_CONF_DIR}/ups.conf', 'w') as f:
             f.write("pollinterval = 1\n")
             f.write("maxretry = 3\n\n")
             f.write("[ups]\n")
             # driver_config should be lines like "driver = usbhid-ups" etc.
             f.write(driver_config + "\n")
             f.write("desc = EthOS Auto Configured UPS\n")
        
        # Enable NET server mode if needed
        with open(f'{NUT_CONF_DIR}/upsd.conf', 'w') as f:
            f.write("LISTEN 0.0.0.0 3493\n")
            f.write("LISTEN ::0 3493\n")

        # Restart NUT
        subprocess.run(['systemctl', 'restart', 'nut-server'], timeout=10)
        subprocess.run(['systemctl', 'restart', 'nut-monitor'], timeout=10)
        
        # Update settings to enabled
        s = load_settings()
        s['enabled'] = True
        save_settings(s)
        
        return jsonify({'ok': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

