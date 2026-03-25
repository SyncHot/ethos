"""
EthOS — Remote Log Reporter

Sends diagnostic logs to a central server for remote debugging.
Collects: boot logs, service journals, system info, eventlog errors.

Default endpoint: https://nas.myserver.pl/api/device-logs
"""

import json
import os
import platform
import re as _re_mod
import shutil as _shutil
import subprocess
import threading
import time
import uuid
from datetime import datetime

from flask import Blueprint, request, jsonify

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path, log_path, ETHOS_ROOT
from utils import load_json as _load_json, save_json as _save_json, register_pkg_routes, require_tools, check_tool
from blueprints.remote_log_db import (
    init_db, save_log, get_devices, get_device_logs, get_log_content, 
    get_latest_log, delete_device_logs as db_delete_device_logs, 
    delete_log as db_delete_log
)

remote_log_bp = Blueprint('remote_log', __name__)

# ── Config ──
CONFIG_FILE = data_path('remote_log.json')
DEVICE_ID_FILE = os.path.join(ETHOS_ROOT, '.device_id')

DEFAULT_CONFIG = {
    'enabled': True,
    'server_url': 'https://nas.myserver.pl/api/device-logs',
    'interval_minutes': 60,
    'send_on_boot': True,
    'send_on_error': True,
    'log_categories': ['boot', 'services', 'system', 'errors', 'dmesg'],
}

_config = {}
_lock = threading.Lock()
_last_send_ts = 0
_send_count = 0
_last_error = ''
_socketio = None


# ── Helpers ──

def _run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ''


def _get_device_id():
    """Persistent unique device identifier."""
    if os.path.isfile(DEVICE_ID_FILE):
        try:
            return open(DEVICE_ID_FILE).read().strip()
        except Exception:
            pass
    # Try machine-id
    for path in ['/etc/machine-id', '/var/lib/dbus/machine-id']:
        if os.path.isfile(path):
            try:
                mid = open(path).read().strip()
                if mid:
                    return mid[:32]
            except Exception:
                pass
    # Generate one
    did = uuid.uuid4().hex
    try:
        os.makedirs(os.path.dirname(DEVICE_ID_FILE), exist_ok=True)
        with open(DEVICE_ID_FILE, 'w') as f:
            f.write(did)
    except Exception:
        pass
    return did


def _load_config():
    global _config
    cfg = dict(DEFAULT_CONFIG)
    saved = _load_json(CONFIG_FILE, None)
    if saved:
        cfg.update(saved)
    _config = cfg
    return cfg


def _save_config():
    try:
        _save_json(CONFIG_FILE, _config)
    except Exception:
        pass


# ── Log Collectors ──

def _collect_system_info():
    """Basic system information."""
    ip = _run("hostname -I 2>/dev/null").split()[0] if _run("hostname -I 2>/dev/null") else ''
    uptime_s = 0
    try:
        uptime_s = float(open('/proc/uptime').read().split()[0])
    except Exception:
        pass

    version = '0.0.0'
    try:
        vf = os.path.join(os.path.dirname(__file__), '..', 'version.json')
        with open(vf) as f:
            version = json.load(f).get('version', version)
    except Exception:
        pass

    installed_at = ''
    marker = os.path.join(ETHOS_ROOT, '.installed')
    if os.path.isfile(marker):
        try:
            for line in open(marker):
                if line.startswith('installed='):
                    installed_at = line.split('=', 1)[1].strip()
        except Exception:
            pass

    return {
        'hostname': _run('hostname'),
        'ip': ip,
        'kernel': platform.release(),
        'arch': platform.machine(),
        'uptime_seconds': int(uptime_s),
        'ethos_version': version,
        'installed_at': installed_at,
        'disk_usage': _run("df -h / 2>/dev/null | awk 'NR==2{print $3\"/\"$2\" (\"$5\")\"}'"),
        'ram_total_mb': _run("free -m 2>/dev/null | awk '/Mem:/{print $2}'"),
        'ram_used_mb': _run("free -m 2>/dev/null | awk '/Mem:/{print $3}'"),
    }


def _collect_boot_log():
    """Firstboot log content."""
    log_file = '/var/log/ethos-firstboot.log'
    if not os.path.isfile(log_file):
        return None
    try:
        with open(log_file) as f:
            content = f.read()
        # Limit to last 200 lines
        lines = content.splitlines()
        if len(lines) > 200:
            lines = lines[-200:]
        return '\n'.join(lines)
    except Exception:
        return None


def _collect_service_journals():
    """Recent journal entries for EthOS services."""
    services = ['ethos', 'ethos-firstboot', 'ethos-preboot', 'ethos-ap']
    result = {}
    for svc in services:
        journal = _run(f'journalctl -u {svc} --no-pager -n 50 --no-hostname 2>/dev/null')
        if journal and 'No entries' not in journal:
            result[svc] = journal
    return result


def _collect_dmesg():
    """Last 50 lines of kernel messages."""
    return _run('dmesg --time-format iso 2>/dev/null | tail -50') or _run('dmesg | tail -50')


def _collect_errors():
    """Recent error-level events from EthOS eventlog."""
    log_file = os.path.join(log_path(), 'eventlog.jsonl')
    if not os.path.isfile(log_file):
        return []
    errors = []
    try:
        with open(log_file) as f:
            for line in f:
                try:
                    evt = json.loads(line.strip())
                    if evt.get('level') in ('error', 'warning'):
                        errors.append(evt)
                except Exception:
                    pass
        # Last 50 errors
        return errors[-50:]
    except Exception:
        return []


def _collect_service_status():
    """Current status of all EthOS services."""
    services = ['ethos', 'ethos-firstboot', 'ethos-preboot', 'ethos-ap',
                'NetworkManager', 'avahi-daemon']
    result = {}
    for svc in services:
        enabled = _run(f'systemctl is-enabled {svc} 2>/dev/null') or 'unknown'
        active = _run(f'systemctl is-active {svc} 2>/dev/null') or 'unknown'
        result[svc] = {'enabled': enabled, 'active': active}
    return result


def _collect_network_info():
    """Network interfaces and connectivity."""
    return {
        'interfaces': _run('nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status 2>/dev/null'),
        'wifi_saved': _run('nmcli -t -f NAME,TYPE connection show 2>/dev/null'),
        'internet': _run('ping -c 1 -W 3 8.8.8.8 2>/dev/null && echo OK || echo FAIL'),
        'dns': _run('ping -c 1 -W 3 google.com 2>/dev/null && echo OK || echo FAIL'),
    }


# ── Send ──

def _build_report(reason='periodic'):
    """Build a full diagnostic report."""
    cats = _config.get('log_categories', DEFAULT_CONFIG['log_categories'])

    report = {
        'device_id': _get_device_id(),
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'reason': reason,  # boot | periodic | error | manual
        'system': _collect_system_info(),
        'services': _collect_service_status(),
        'network': _collect_network_info(),
    }

    if 'boot' in cats:
        boot_log = _collect_boot_log()
        if boot_log:
            report['boot_log'] = boot_log

    if 'services' in cats:
        report['journals'] = _collect_service_journals()

    if 'dmesg' in cats:
        dmesg = _collect_dmesg()
        if dmesg:
            report['dmesg'] = dmesg

    if 'errors' in cats:
        errors = _collect_errors()
        if errors:
            report['errors'] = errors

    return report


def send_report(reason='periodic'):
    """Collect & send logs to remote server. Returns (ok, message)."""
    global _last_send_ts, _send_count, _last_error

    if not _config.get('enabled'):
        return False, 'Remote logging disabled'

    url = _config.get('server_url', DEFAULT_CONFIG['server_url'])
    if not url:
        return False, 'No server URL configured'

    try:
        report = _build_report(reason)
        payload = json.dumps(report, ensure_ascii=False)

        # Use urllib to avoid extra dependency
        import urllib.request
        import urllib.error

        req = urllib.request.Request(
            url,
            data=payload.encode('utf-8'),
            headers={
                'Content-Type': 'application/json',
                'X-Device-ID': report['device_id'],
                'X-EthOS-Version': report['system'].get('ethos_version', '?'),
            },
            method='POST',
        )
        # 30s timeout
        resp = urllib.request.urlopen(req, timeout=30)
        status = resp.getcode()

        with _lock:
            _last_send_ts = time.time()
            _send_count += 1
            _last_error = ''

        return True, f'Sent ({status})'

    except urllib.error.HTTPError as e:
        msg = f'HTTP {e.code}: {e.reason}'
        with _lock:
            _last_error = msg
        return False, msg
    except Exception as e:
        msg = str(e)[:200]
        with _lock:
            _last_error = msg
        return False, msg


# ── Background loop ──

def _remote_log_loop():
    """Background loop: send on boot, then periodically."""
    # Wait for app to stabilize
    time.sleep(30)

    # Load config
    _load_config()

    if not _config.get('enabled'):
        return

    # Send boot report
    if _config.get('send_on_boot'):
        ok, msg = send_report('boot')
        print(f'[remote_log] Boot report: {msg}')

    # Periodic loop
    while True:
        interval = max(_config.get('interval_minutes', 60), 5) * 60
        time.sleep(interval)
        if _config.get('enabled'):
            ok, msg = send_report('periodic')
            if not ok:
                print(f'[remote_log] Send failed: {msg}')


def init_remote_log(socketio_instance):
    """Initialize remote logging background task."""
    global _socketio
    _socketio = socketio_instance
    init_db()
    _load_config()
    _save_config()  # Ensure defaults written
    socketio_instance.start_background_task(_remote_log_loop)


# ── API Endpoints ──

@remote_log_bp.route('/api/remote-log/config')
def remote_log_config():
    """GET current remote logging configuration."""
    _load_config()
    return jsonify({
        **_config,
        'device_id': _get_device_id(),
        'last_send': _last_send_ts,
        'send_count': _send_count,
        'last_error': _last_error,
    })


@remote_log_bp.route('/api/remote-log/config', methods=['POST'])
def remote_log_config_update():
    """POST update remote logging config."""
    data = request.get_json() or {}
    with _lock:
        if 'enabled' in data:
            _config['enabled'] = bool(data['enabled'])
        if 'server_url' in data:
            _config['server_url'] = str(data['server_url']).strip()
        if 'interval_minutes' in data:
            _config['interval_minutes'] = max(int(data['interval_minutes']), 5)
        if 'send_on_boot' in data:
            _config['send_on_boot'] = bool(data['send_on_boot'])
        if 'send_on_error' in data:
            _config['send_on_error'] = bool(data['send_on_error'])
        if 'log_categories' in data and isinstance(data['log_categories'], list):
            _config['log_categories'] = data['log_categories']
        _save_config()
    return jsonify({'ok': True, **_config})


@remote_log_bp.route('/api/remote-log/send', methods=['POST'])
def remote_log_send_now():
    """POST manually trigger a log report."""
    err = require_tools('nmcli')
    if err:
        return err
    ok, msg = send_report('manual')
    return jsonify({'ok': ok, 'message': msg})


@remote_log_bp.route('/api/remote-log/preview')
def remote_log_preview():
    """GET preview what would be sent (without actually sending)."""
    err = require_tools('nmcli')
    if err:
        return err
    _load_config()
    report = _build_report('preview')
    return jsonify(report)


@remote_log_bp.route('/api/remote-log/status')
def remote_log_status():
    """GET quick status of remote logging."""
    return jsonify({
        'enabled': _config.get('enabled', False),
        'server_url': _config.get('server_url', ''),
        'device_id': _get_device_id(),
        'last_send': _last_send_ts,
        'send_count': _send_count,
        'last_error': _last_error,
    })


# ═══════════════════════════════════════════════════════════
#  Receiver — accept logs from other EthOS devices
# ═══════════════════════════════════════════════════════════

@remote_log_bp.route('/api/device-logs', methods=['POST'])
def receive_device_logs():
    """Receive diagnostic report from a remote EthOS device."""
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({'error': 'Invalid JSON'}), 400

    try:
        filename = save_log(data)
        
        # Log to console
        device_id = data.get('device_id', 'unknown')
        reason = data.get('reason', 'unknown')
        sys_info = data.get('system', {})
        hostname = sys_info.get('hostname', '?')
        version = sys_info.get('ethos_version', '?')
        ip = sys_info.get('ip', '?')
        errors = data.get('errors', [])
        services = data.get('services', {})
        failed = [s for s, v in services.items() if isinstance(v, dict) and v.get('active') == 'failed']

        print(f'[device-log] {device_id[:12]}.. | {hostname} | {ip} | v{version} | {reason}'
              + (f' | {len(errors)} err' if errors else '')
              + (f' | FAIL: {",".join(failed)}' if failed else ''))

        return jsonify({'ok': True, 'saved': filename})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@remote_log_bp.route('/api/device-logs', methods=['GET'])
def list_device_logs():
    """List all devices with latest report summary."""
    devices = get_devices()
    return jsonify({'devices': devices})


@remote_log_bp.route('/api/device-logs/<device_id>')
def device_log_detail(device_id):
    """Get latest report for a device."""
    log = get_latest_log(device_id)
    if not log:
        return jsonify({'error': 'Device not found'}), 404
    return jsonify(log)


@remote_log_bp.route('/api/device-logs/<device_id>/history')
def device_log_history(device_id):
    """List all reports for a device."""
    reports = get_device_logs(device_id)
    # If no reports but device exists? get_device_logs returns [] if device not found or no logs
    # But for API consistency we might want 404 if device strictly doesn't exist?
    # For now, empty list is fine or we can check if reports is empty
    if not reports:
         # Double check if device exists at all? 
         # Optimization: just return empty list or 404 if user expects 404
         pass
         
    return jsonify({'device_id': device_id, 'reports': reports})


@remote_log_bp.route('/api/device-logs/<device_id>/<filename>')
def device_log_report(device_id, filename):
    """Get a specific historical report."""
    content = get_log_content(device_id, filename)
    if not content:
        return jsonify({'error': 'Not found'}), 404
    return jsonify(content)


@remote_log_bp.route('/api/device-logs/<device_id>', methods=['DELETE'])
def delete_device_logs(device_id):
    """Delete all logs for a device."""
    db_delete_device_logs(device_id)
    return jsonify({'ok': True})


@remote_log_bp.route('/api/device-logs/<device_id>/<filename>', methods=['DELETE'])
def delete_device_report(device_id, filename):
    """Delete a specific report."""
    db_delete_log(device_id, filename)
    return jsonify({'ok': True})


# ── Package: install / uninstall / status ──

register_pkg_routes(
    remote_log_bp,
    install_message='Remote logs ready.',
    wipe_files=[CONFIG_FILE, data_path('remote_logs.db')],
    wipe_dirs=[],
    url_prefix='/api/remote-log',
)
