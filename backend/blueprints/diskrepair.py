"""
EthOS — Disk Repair Blueprint
Diagnose disk health, run filesystem checks, detect bad sectors,
and repair corrupted filesystems.
"""

import json
import os
import re
import signal
import subprocess
import threading
_HELPER = '/opt/ethos/tools/ethos-system-helper.sh'
import time

from flask import Blueprint, jsonify, request

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import host_run, data_path, q, ensure_dep
from utils import is_pid_alive, load_json as _load_json, save_json as _save_json, register_pkg_routes, require_tools, check_tool

diskrepair_bp = Blueprint('diskrepair', __name__, url_prefix='/api/diskrepair')

# ---------------------------------------------------------------------------
# Repair State (persistent across reconnects)
# ---------------------------------------------------------------------------
_REPAIR_STATE_FILE = data_path('diskrepair_state.json')
_HISTORY_FILE = data_path('diskrepair_history.json')
_MAX_STATE_LOGS = 500
_MAX_HISTORY = 100

_repair_state = {
    'status': 'idle',       # idle | running | done | error
    'operation': '',        # smart | fsck | badblocks | repair
    'disk': '',
    'partition': '',
    'percent': 0,
    'message': '',
    'logs': [],
    'pid': 0,
    'start_time': 0,
    'result': None,
}
_repair_lock = threading.Lock()


def _save_state():
    try:
        _save_json(_REPAIR_STATE_FILE, _repair_state)
    except Exception:
        pass


def _load_state():
    global _repair_state
    try:
        saved = _load_json(_REPAIR_STATE_FILE, None)
        if saved is None:
            return
        _repair_state.update(saved)
        if _repair_state['status'] == 'running':
            pid = _repair_state.get('pid', 0)
            if not pid or not is_pid_alive(pid):
                _repair_state['status'] = 'error'
                _repair_state['message'] = 'Operation interrupted (service restart)'
                _repair_state['result'] = {'success': False, 'message': _repair_state['message']}
                _save_state()
    except Exception:
        pass


def _is_pid_alive(pid):
    return is_pid_alive(pid)


def _update_state(**kw):
    with _repair_lock:
        _repair_state.update(kw)
        if 'log_line' in kw:
            line = kw.pop('log_line')
            _repair_state.pop('log_line', None)
            _repair_state['logs'].append(line)
            if len(_repair_state['logs']) > _MAX_STATE_LOGS:
                _repair_state['logs'] = _repair_state['logs'][-_MAX_STATE_LOGS:]
        _save_state()


def _reset_state():
    with _repair_lock:
        _repair_state.update({
            'status': 'idle', 'operation': '', 'disk': '', 'partition': '',
            'percent': 0, 'message': '', 'logs': [], 'pid': 0,
            'start_time': 0, 'result': None,
        })
        _save_state()


def _save_history(entry):
    try:
        history = []
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE) as f:
                history = json.load(f)
        history.append(entry)
        history = history[-_MAX_HISTORY:]
        with open(_HISTORY_FILE, 'w') as f:
            json.dump(history, f)
    except Exception:
        pass


_load_state()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_NAME_RE = re.compile(r'^[a-zA-Z0-9]+$')
_SYSTEM_MOUNTS = ('/', '/boot', '/boot/efi')


def _validate_name(name):
    return bool(name and _NAME_RE.match(name))


def _dev_exists(name):
    return os.path.exists(f'/dev/{name}')


def _parse_size_bytes(name):
    r = host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh blockdev --getsize64 /dev/{name} 2>/dev/null", timeout=5)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        return int(r.stdout.strip())
    return 0


def _smartctl_available():
    r = host_run("which smartctl 2>/dev/null", timeout=5)
    return r.returncode == 0


def _ensure_smartctl():
    ensure_dep('smartctl', install=True)


def _get_mountpoint(partition):
    r = host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -nlo MOUNTPOINT /dev/{partition} 2>/dev/null", timeout=5)
    if r.returncode == 0:
        mp = r.stdout.strip().splitlines()
        return mp[0].strip() if mp and mp[0].strip() else None
    return None


def _is_system_partition(partition):
    mp = _get_mountpoint(partition)
    return mp in _SYSTEM_MOUNTS if mp else False


def _try_smartctl(disk_name, timeout=15):
    """Run smartctl and return parsed JSON, trying -d sat for USB disks on failure.

    smartctl uses bitmask exit codes — JSON output is usually valid even on
    non-zero return.  We always try to parse the output regardless of exitcode.
    Returns parsed dict or None.
    """
    for extra in ('', '-d sat'):
        cmd = f"sudo /opt/ethos/tools/ethos-system-helper.sh smartctl -j -a {extra} /dev/{disk_name} 2>/dev/null".strip()
        sr = host_run(cmd, timeout=timeout)
        if sr.stdout and sr.stdout.strip():
            try:
                sdata = json.loads(sr.stdout)
                # Check for fatal errors (permission denied, device open failed)
                msgs = sdata.get('smartctl', {}).get('messages', [])
                if any('Permission denied' in m.get('string', '') for m in msgs):
                    return None
                # If we got smart_support info, it's valid
                if sdata.get('smart_support') or sdata.get('smart_status'):
                    return sdata
                # For NVMe, check device protocol
                if sdata.get('device', {}).get('protocol') == 'NVMe':
                    return sdata
                # First attempt may fail for USB — try -d sat
                if not extra:
                    continue
                return sdata
            except (json.JSONDecodeError, TypeError):
                continue
    return None


# ---------------------------------------------------------------------------
# 1. GET /disks — List all disks with health overview
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/disks')
def list_disks():
    err = require_tools('smartctl')
    if err:
        return err
    r = host_run(
        "sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -J -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL,TRAN,ROTA,RO,STATE,HCTL 2>/dev/null"
    )
    if r.returncode != 0:
        return jsonify({'error': 'Cannot read disk list'}), 500

    try:
        data = json.loads(r.stdout)
    except (json.JSONDecodeError, TypeError):
        return jsonify({'error': 'Failed to parse lsblk output'}), 500

    has_smart = _smartctl_available()
    disks = []

    for dev in data.get('blockdevices', []):
        if dev.get('type') != 'disk':
            continue

        name = dev.get('name', '')
        size_bytes = _parse_size_bytes(name)

        disk_info = {
            'name': name,
            'model': (dev.get('model') or '').strip(),
            'serial': (dev.get('serial') or '').strip(),
            'size': dev.get('size', ''),
            'size_bytes': size_bytes,
            'transport': dev.get('tran') or '',
            'rotational': dev.get('rota') in (True, '1', 1),
            'state': dev.get('state') or '',
            'smart_available': False,
            'smart_healthy': None,
            'temperature': None,
            'power_on_hours': None,
            'reallocated_sectors': None,
            'pending_sectors': None,
            'partitions': [],
        }

        # Gather partitions
        for ch in dev.get('children', []):
            mp = ch.get('mountpoint') or None
            disk_info['partitions'].append({
                'name': ch.get('name', ''),
                'size': ch.get('size', ''),
                'fstype': ch.get('fstype') or '',
                'mountpoint': mp,
                'mounted': bool(mp),
            })

        # SMART data (quick check) — try parsing JSON regardless of exit code
        # smartctl uses bitmask exit codes; JSON output is often valid even on non-zero
        if has_smart:
            sdata = _try_smartctl(name)
            if sdata:
                disk_info['smart_available'] = sdata.get('smart_support', {}).get('available', False)
                health = sdata.get('smart_status', {})
                disk_info['smart_healthy'] = health.get('passed', None)
                temp = sdata.get('temperature', {})
                disk_info['temperature'] = temp.get('current', None)
                disk_info['power_on_hours'] = sdata.get('power_on_time', {}).get('hours', None)
                for attr in sdata.get('ata_smart_attributes', {}).get('table', []):
                    aid = attr.get('id')
                    raw_val = attr.get('raw', {}).get('value', 0)
                    if aid == 5:
                        disk_info['reallocated_sectors'] = raw_val
                    elif aid == 197:
                        disk_info['pending_sectors'] = raw_val

        disks.append(disk_info)

    return jsonify(disks)


# ---------------------------------------------------------------------------
# 2. GET /smart/<disk> — Full SMART data for a disk
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/smart/<disk>')
def smart_detail(disk):
    err = require_tools('smartctl')
    if err:
        return err
    if not _validate_name(disk):
        return jsonify({'error': 'Invalid disk name'}), 400
    if not _dev_exists(disk):
        return jsonify({'error': f'/dev/{disk} not found'}), 404

    _ensure_smartctl()

    sdata = _try_smartctl(disk, timeout=30)
    if not sdata:
        return jsonify({'error': 'SMART not available for this disk (not supported or access denied)'}), 400

    device = sdata.get('device', {})
    info = {
        'model': sdata.get('model_name', ''),
        'serial': sdata.get('serial_number', ''),
        'firmware': sdata.get('firmware_version', ''),
        'capacity': sdata.get('user_capacity', {}).get('bytes', 0),
        'device_type': device.get('type', ''),
        'protocol': device.get('protocol', ''),
    }

    health = sdata.get('smart_status', {})

    attributes = []
    for attr in sdata.get('ata_smart_attributes', {}).get('table', []):
        flags = attr.get('flags', {})
        status = 'ok'
        thresh = attr.get('thresh', 0)
        worst = attr.get('worst', 0)
        if thresh and worst and worst <= thresh:
            status = 'failing'
        elif attr.get('when_failed', '') not in ('', '-'):
            status = 'warn'
        attributes.append({
            'id': attr.get('id'),
            'name': attr.get('name', ''),
            'value': attr.get('value', 0),
            'worst': worst,
            'thresh': thresh,
            'raw': attr.get('raw', {}).get('string', str(attr.get('raw', {}).get('value', ''))),
            'status': status,
        })

    temperature = sdata.get('temperature', {}).get('current', None)
    power_on_hours = sdata.get('power_on_time', {}).get('hours', None)
    power_cycle_count = sdata.get('power_cycle_count', None)

    self_test_log = []
    for entry in sdata.get('ata_smart_self_test_log', {}).get('standard', {}).get('table', []):
        self_test_log.append({
            'type': entry.get('type', {}).get('string', ''),
            'status': entry.get('status', {}).get('string', ''),
            'remaining_percent': entry.get('status', {}).get('remaining_percent', 0),
            'lifetime_hours': entry.get('lifetime_hours', 0),
        })

    # Error log from text output
    error_log = []
    er = host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh smartctl -l error /dev/{disk} 2>/dev/null", timeout=15)
    if er.returncode == 0 and er.stdout:
        error_log = [line for line in er.stdout.strip().splitlines() if line.strip()]

    return jsonify({
        'info': info,
        'health': {
            'passed': health.get('passed', None),
            'string': 'PASSED' if health.get('passed') else 'FAILED',
        },
        'attributes': attributes,
        'temperature': temperature,
        'power_on_hours': power_on_hours,
        'power_cycle_count': power_cycle_count,
        'error_log': error_log,
        'self_test_log': self_test_log,
    })


# ---------------------------------------------------------------------------
# 3. POST /fsck — Run filesystem check
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/fsck', methods=['POST'])
def run_fsck():
    data = request.json or {}
    partition = data.get('partition', '').strip()
    repair = bool(data.get('repair', False))

    if not _validate_name(partition):
        return jsonify({'error': 'Invalid partition name'}), 400
    if not _dev_exists(partition):
        return jsonify({'error': f'/dev/{partition} not found'}), 404

    with _repair_lock:
        if _repair_state['status'] == 'running':
            return jsonify({'error': 'Another operation is already running'}), 409

    mountpoint = _get_mountpoint(partition)
    is_system = _is_system_partition(partition)

    if mountpoint and repair:
        if is_system:
            return jsonify({'error': f'Cannot repair system partition {partition} (mounted at {mountpoint}). Unmount first.'}), 400
        # Auto-unmount for repair
        ur = host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh umount /dev/{partition} 2>&1", timeout=15)
        if ur.returncode != 0:
            return jsonify({'error': f'Cannot unmount /dev/{partition}: {ur.stdout.strip()}'}), 400
        mountpoint = None

    if repair:
        cmd = f"sudo /opt/ethos/tools/ethos-system-helper.sh fsck /dev/{partition} 2>&1"
    elif mountpoint:
        cmd = f"sudo /opt/ethos/tools/ethos-system-helper.sh fsck-check /dev/{partition} 2>&1"
    else:
        cmd = f"sudo /opt/ethos/tools/ethos-system-helper.sh fsck-check /dev/{partition} 2>&1"

    _update_state(
        status='running', operation='fsck', disk='', partition=partition,
        percent=0, message=f'Running fsck on /dev/{partition}...',
        logs=[], pid=0, start_time=time.time(), result=None,
    )

    t = threading.Thread(target=_fsck_worker, args=(partition, cmd, repair), daemon=True)
    t.start()

    return jsonify({'ok': True, 'repair': repair, 'partition': partition})


def _fsck_worker(partition, cmd, repair):
    start = time.time()
    try:
        _update_state(log_line=f'$ {cmd}')
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        _update_state(pid=proc.pid)

        for line in proc.stdout:
            line = line.rstrip('\n')
            if line.strip():
                _update_state(log_line=line)
            # Rough progress from e2fsck pass indicators
            if 'Pass 1' in line:
                _update_state(percent=10, message='Pass 1: Checking inodes, blocks, sizes')
            elif 'Pass 2' in line:
                _update_state(percent=30, message='Pass 2: Checking directory structure')
            elif 'Pass 3' in line:
                _update_state(percent=50, message='Pass 3: Checking directory connectivity')
            elif 'Pass 4' in line:
                _update_state(percent=70, message='Pass 4: Checking reference counts')
            elif 'Pass 5' in line:
                _update_state(percent=85, message='Pass 5: Checking group summary')

        proc.wait()
        code = proc.returncode
        elapsed = time.time() - start

        # fsck exit codes: 0=clean, 1=corrected, 2=reboot needed, 4+=errors
        if code == 0:
            msg = f'Filesystem clean — no errors found ({elapsed:.1f}s)'
            _update_state(status='done', percent=100, message=msg,
                          result={'success': True, 'message': msg, 'exit_code': code})
        elif code == 1:
            msg = f'Errors corrected successfully ({elapsed:.1f}s)'
            _update_state(status='done', percent=100, message=msg,
                          result={'success': True, 'message': msg, 'exit_code': code})
        elif code == 2:
            msg = f'Errors corrected — system reboot required ({elapsed:.1f}s)'
            _update_state(status='done', percent=100, message=msg,
                          result={'success': True, 'message': msg, 'exit_code': code, 'reboot_needed': True})
        else:
            msg = f'fsck finished with errors (exit code {code}, {elapsed:.1f}s)'
            _update_state(status='error', percent=100, message=msg,
                          result={'success': False, 'message': msg, 'exit_code': code})

        _save_history({
            'operation': 'fsck', 'partition': partition, 'repair': repair,
            'exit_code': code, 'success': code in (0, 1, 2),
            'message': msg, 'timestamp': time.time(), 'elapsed': round(elapsed),
        })
    except Exception as exc:
        elapsed = time.time() - start
        msg = f'fsck error: {exc}'
        _update_state(status='error', message=msg, result={'success': False, 'message': msg})
        _save_history({
            'operation': 'fsck', 'partition': partition, 'repair': repair,
            'exit_code': -1, 'success': False,
            'message': msg, 'timestamp': time.time(), 'elapsed': round(elapsed),
        })


# ---------------------------------------------------------------------------
# 4. POST /badblocks — Scan for bad blocks
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/badblocks', methods=['POST'])
def run_badblocks():
    data = request.json or {}
    disk = data.get('disk', '').strip()
    mode = data.get('mode', 'readonly').strip()

    if not _validate_name(disk):
        return jsonify({'error': 'Invalid disk name'}), 400
    if not _dev_exists(disk):
        return jsonify({'error': f'/dev/{disk} not found'}), 404
    if mode not in ('readonly', 'nondestructive'):
        return jsonify({'error': 'Mode must be readonly or nondestructive'}), 400

    with _repair_lock:
        if _repair_state['status'] == 'running':
            return jsonify({'error': 'Another operation is already running'}), 409

    ensure_dep('badblocks', install=True)

    if mode == 'nondestructive':
        cmd = f"badblocks -nsv /dev/{disk} 2>&1"
    else:
        cmd = f"badblocks -sv /dev/{disk} 2>&1"

    _update_state(
        status='running', operation='badblocks', disk=disk, partition='',
        percent=0, message=f'Scanning /dev/{disk} for bad blocks ({mode})...',
        logs=[], pid=0, start_time=time.time(), result=None,
    )

    t = threading.Thread(target=_badblocks_worker, args=(disk, cmd, mode), daemon=True)
    t.start()

    return jsonify({'ok': True, 'disk': disk, 'mode': mode})


def _badblocks_worker(disk, cmd, mode):
    start = time.time()
    bad_count = 0
    try:
        _update_state(log_line=f'$ {cmd}')
        # badblocks writes progress to stderr
        proc = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        _update_state(pid=proc.pid)

        for line in proc.stdout:
            line = line.rstrip('\n\r')
            if not line.strip():
                continue

            # Parse progress: "Testing with pattern 0xaa: 23.45% done, 1:23 elapsed. (0/0/0 errors)"
            m = re.search(r'([\d.]+)%\s*done', line)
            if m:
                pct = min(99, int(float(m.group(1))))
                _update_state(percent=pct, message=line.strip())

            # Count bad blocks in output (lines that are just numbers)
            if re.match(r'^\d+\s*$', line.strip()):
                bad_count += 1

            _update_state(log_line=line)

        proc.wait()
        code = proc.returncode
        elapsed = time.time() - start

        if code == 0:
            if bad_count > 0:
                msg = f'Scan complete: {bad_count} bad block(s) found ({elapsed:.0f}s)'
            else:
                msg = f'Scan complete: no bad blocks found ({elapsed:.0f}s)'
            _update_state(status='done', percent=100, message=msg,
                          result={'success': True, 'message': msg, 'bad_blocks': bad_count})
        else:
            msg = f'badblocks finished with error (exit code {code}, {elapsed:.0f}s)'
            _update_state(status='error', percent=100, message=msg,
                          result={'success': False, 'message': msg, 'bad_blocks': bad_count})

        _save_history({
            'operation': 'badblocks', 'disk': disk, 'mode': mode,
            'bad_blocks': bad_count, 'success': code == 0,
            'message': msg, 'timestamp': time.time(), 'elapsed': round(elapsed),
        })
    except Exception as exc:
        elapsed = time.time() - start
        msg = f'badblocks error: {exc}'
        _update_state(status='error', message=msg, result={'success': False, 'message': msg})
        _save_history({
            'operation': 'badblocks', 'disk': disk, 'mode': mode,
            'bad_blocks': bad_count, 'success': False,
            'message': msg, 'timestamp': time.time(), 'elapsed': round(elapsed),
        })


# ---------------------------------------------------------------------------
# 5. POST /smart-test — Start SMART self-test
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/smart-test', methods=['POST'])
def start_smart_test():
    err = require_tools('smartctl')
    if err:
        return err
    data = request.json or {}
    disk = data.get('disk', '').strip()
    test_type = data.get('type', 'short').strip()

    if not _validate_name(disk):
        return jsonify({'error': 'Invalid disk name'}), 400
    if not _dev_exists(disk):
        return jsonify({'error': f'/dev/{disk} not found'}), 404
    if test_type not in ('short', 'long'):
        return jsonify({'error': 'Type must be short or long'}), 400

    _ensure_smartctl()

    r = host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh smartctl -t {test_type} /dev/{disk} 2>&1", timeout=15)
    output = r.stdout.strip() if r.stdout else ''

    # Parse estimated completion time
    est_time = None
    m = re.search(r'Please wait (\d+) minutes', output)
    if m:
        est_time = int(m.group(1))

    if r.returncode in (0, 4):
        _save_history({
            'operation': 'smart-test', 'disk': disk, 'type': test_type,
            'success': True, 'message': f'{test_type} self-test started',
            'timestamp': time.time(), 'elapsed': 0,
        })
        return jsonify({
            'ok': True,
            'disk': disk,
            'type': test_type,
            'estimated_minutes': est_time,
            'output': output,
        })

    return jsonify({'error': f'smartctl failed: {output}'}), 500


# ---------------------------------------------------------------------------
# 6. SMART Health Score & Prediction
# ---------------------------------------------------------------------------

import sqlite3

_SMART_DB = data_path('smart_history.db')

# Critical SMART attributes and their weights for health scoring
_CRITICAL_ATTRS = {
    5:   ('Reallocated_Sector_Ct',   30),   # Most critical
    187: ('Reported_Uncorrect',       20),
    188: ('Command_Timeout',          5),
    196: ('Reallocated_Event_Count',  15),
    197: ('Current_Pending_Sector',   20),
    198: ('Offline_Uncorrectable',    15),
    10:  ('Spin_Retry_Count',         10),
    184: ('End-to-End_Error',         10),
    199: ('UDMA_CRC_Error_Count',     5),
    201: ('Soft_Read_Error_Rate',     5),
}


def _get_smart_db():
    os.makedirs(os.path.dirname(_SMART_DB), exist_ok=True)
    conn = sqlite3.connect(_SMART_DB, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute('''
        CREATE TABLE IF NOT EXISTS smart_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            disk TEXT NOT NULL,
            ts REAL NOT NULL,
            health_score INTEGER,
            temperature INTEGER,
            power_on_hours INTEGER,
            attributes_json TEXT,
            health_passed INTEGER
        )
    ''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_smart_disk_ts ON smart_snapshots(disk, ts)')
    conn.commit()
    return conn


def _calc_health_score(attributes):
    """Calculate health score 0-100 from SMART attributes."""
    score = 100
    for attr in attributes:
        attr_id = attr.get('id')
        if attr_id not in _CRITICAL_ATTRS:
            continue

        name, weight = _CRITICAL_ATTRS[attr_id]
        raw_str = str(attr.get('raw', '0'))
        try:
            raw_val = int(raw_str.split()[0])
        except (ValueError, IndexError):
            raw_val = 0

        if raw_val == 0:
            continue

        # Deduct points based on severity
        if attr_id in (5, 196):  # Reallocated sectors
            if raw_val >= 100:
                score -= weight
            elif raw_val >= 10:
                score -= weight * 0.7
            elif raw_val >= 1:
                score -= weight * 0.3
        elif attr_id in (197, 198):  # Pending/Uncorrectable
            if raw_val >= 50:
                score -= weight
            elif raw_val >= 5:
                score -= weight * 0.6
            elif raw_val >= 1:
                score -= weight * 0.3
        elif attr_id == 187:  # Reported uncorrectable
            if raw_val >= 100:
                score -= weight
            elif raw_val >= 10:
                score -= weight * 0.5
            elif raw_val >= 1:
                score -= weight * 0.2
        else:
            if raw_val >= 100:
                score -= weight
            elif raw_val >= 10:
                score -= weight * 0.5
            elif raw_val >= 1:
                score -= weight * 0.15

        # Failing threshold check
        if attr.get('status') == 'failing':
            score -= 20

    return max(0, min(100, int(score)))


def _calc_nvme_health_score(sdata):
    """Calculate health score for NVMe drives."""
    score = 100
    pct_used = sdata.get('nvme_smart_health_information_log', {}).get('percentage_used', 0)
    spare = sdata.get('nvme_smart_health_information_log', {}).get('available_spare', 100)
    spare_thresh = sdata.get('nvme_smart_health_information_log', {}).get('available_spare_threshold', 10)
    media_errors = sdata.get('nvme_smart_health_information_log', {}).get('media_errors', 0)
    crit_warn = sdata.get('nvme_smart_health_information_log', {}).get('critical_warning', 0)

    if pct_used > 100:
        score -= 30
    elif pct_used > 90:
        score -= 15
    elif pct_used > 80:
        score -= 5

    if spare < spare_thresh:
        score -= 30
    elif spare < 20:
        score -= 15

    if media_errors > 0:
        score -= min(25, media_errors * 5)

    if crit_warn > 0:
        score -= 20

    return max(0, min(100, int(score)))


@diskrepair_bp.route('/smart/<disk>/score')
def smart_health_score(disk):
    """Get health score 0-100 for a disk, with snapshot storage."""
    err = require_tools('smartctl')
    if err:
        return err
    if not _validate_name(disk):
        return jsonify({'error': 'Invalid disk name'}), 400
    if not _dev_exists(disk):
        return jsonify({'error': f'/dev/{disk} not found'}), 404

    _ensure_smartctl()
    sdata = _try_smartctl(disk, timeout=30)
    if not sdata:
        return jsonify({'error': 'SMART not available'}), 400

    protocol = sdata.get('device', {}).get('protocol', '')
    health_passed = sdata.get('smart_status', {}).get('passed', None)
    temperature = sdata.get('temperature', {}).get('current', None)
    power_on_hours = sdata.get('power_on_time', {}).get('hours', None)

    if protocol == 'NVMe':
        nvme_log = sdata.get('nvme_smart_health_information_log', {})
        score = _calc_nvme_health_score(sdata)
        attrs_json = json.dumps({
            'percentage_used': nvme_log.get('percentage_used', 0),
            'available_spare': nvme_log.get('available_spare', 100),
            'media_errors': nvme_log.get('media_errors', 0),
            'critical_warning': nvme_log.get('critical_warning', 0),
            'data_units_written': nvme_log.get('data_units_written', 0),
            'data_units_read': nvme_log.get('data_units_read', 0),
            'power_on_hours': nvme_log.get('power_on_hours', 0),
        })
    else:
        attributes = []
        for attr in sdata.get('ata_smart_attributes', {}).get('table', []):
            attributes.append({
                'id': attr.get('id'),
                'name': attr.get('name', ''),
                'value': attr.get('value', 0),
                'worst': attr.get('worst', 0),
                'thresh': attr.get('thresh', 0),
                'raw': attr.get('raw', {}).get('string', str(attr.get('raw', {}).get('value', ''))),
                'status': 'ok',
            })
        score = _calc_health_score(attributes)
        attrs_json = json.dumps(attributes)

    # Store snapshot
    try:
        conn = _get_smart_db()
        conn.execute(
            'INSERT INTO smart_snapshots (disk, ts, health_score, temperature, power_on_hours, attributes_json, health_passed) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (disk, time.time(), score, temperature, power_on_hours, attrs_json, 1 if health_passed else 0)
        )
        conn.commit()
        conn.close()
    except Exception:
        pass

    grade = 'excellent' if score >= 90 else 'good' if score >= 70 else 'warning' if score >= 50 else 'critical'

    return jsonify({
        'ok': True,
        'disk': disk,
        'score': score,
        'grade': grade,
        'health_passed': health_passed,
        'temperature': temperature,
        'power_on_hours': power_on_hours,
        'protocol': protocol,
    })


@diskrepair_bp.route('/smart/<disk>/history')
def smart_history(disk):
    """Get SMART trend history for a disk."""
    if not _validate_name(disk):
        return jsonify({'error': 'Invalid disk name'}), 400

    limit = min(int(request.args.get('limit', 90)), 365)

    try:
        conn = _get_smart_db()
        rows = conn.execute(
            'SELECT ts, health_score, temperature, power_on_hours, health_passed '
            'FROM smart_snapshots WHERE disk = ? ORDER BY ts DESC LIMIT ?',
            (disk, limit)
        ).fetchall()
        conn.close()
    except Exception:
        return jsonify({'ok': True, 'history': []})

    history = [{
        'ts': r['ts'],
        'score': r['health_score'],
        'temperature': r['temperature'],
        'power_on_hours': r['power_on_hours'],
        'health_passed': bool(r['health_passed']),
    } for r in rows]

    history.reverse()

    prediction = None
    if len(history) >= 7:
        scores = [h['score'] for h in history[-7:]]
        avg_decline = (scores[0] - scores[-1]) / 7 if scores[0] != scores[-1] else 0
        if avg_decline > 0:
            days_to_zero = int(scores[-1] / avg_decline) if avg_decline > 0.1 else None
            prediction = {
                'trend': 'declining',
                'avg_daily_decline': round(avg_decline, 2),
                'estimated_days_to_failure': days_to_zero,
            }
        else:
            prediction = {'trend': 'stable'}

    return jsonify({
        'ok': True,
        'disk': disk,
        'history': history,
        'prediction': prediction,
    })


# ---------------------------------------------------------------------------
# 6. GET /status — Current operation status
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/status')
def repair_status():
    try:
        since = int(request.args.get('since', 0))
    except (ValueError, TypeError):
        since = 0
    with _repair_lock:
        out = dict(_repair_state)
        out['logs'] = _repair_state['logs'][since:]
        out['log_offset'] = since
        out['log_total'] = len(_repair_state['logs'])
    return jsonify(out)


# ---------------------------------------------------------------------------
# 7. POST /cancel — Cancel running operation
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/cancel', methods=['POST'])
def cancel_operation():
    with _repair_lock:
        if _repair_state['status'] != 'running':
            return jsonify({'error': 'No operation running'}), 400
        pid = _repair_state.get('pid', 0)

    if pid and pid > 0:
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        except OSError:
            pass

    _update_state(status='error', message='Operation cancelled by user',
                  result={'success': False, 'message': 'Cancelled'})
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# 8. POST /dismiss — Clear completed/error state
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/dismiss', methods=['POST'])
def dismiss():
    _reset_state()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# 9. GET /history — Past repair operations
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/history')
def repair_history():
    try:
        if os.path.exists(_HISTORY_FILE):
            with open(_HISTORY_FILE) as f:
                return jsonify(json.load(f))
    except Exception:
        pass
    return jsonify([])


# ---------------------------------------------------------------------------
# 10. POST /unmount — Unmount a partition
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/unmount', methods=['POST'])
def unmount_partition():
    data = request.json or {}
    partition = data.get('partition', '').strip()

    if not _validate_name(partition):
        return jsonify({'error': 'Invalid partition name'}), 400
    if not _dev_exists(partition):
        return jsonify({'error': f'/dev/{partition} not found'}), 404

    mp = _get_mountpoint(partition)
    if not mp:
        return jsonify({'status': 'ok', 'mounted': False})

    if mp in _SYSTEM_MOUNTS:
        return jsonify({'error': f'Cannot unmount system partition ({mp})'}), 400

    r = host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh umount /dev/{partition} 2>&1", timeout=15)
    if r.returncode == 0:
        return jsonify({'status': 'ok', 'partition': partition, 'mountpoint': mp})

    return jsonify({'error': f'Failed to unmount: {(r.stdout or "").strip()}'}), 500


# ---------------------------------------------------------------------------
# 11. GET /filesystem/<partition> — Detailed filesystem info
# ---------------------------------------------------------------------------

@diskrepair_bp.route('/filesystem/<partition>')
def filesystem_info(partition):
    if not _validate_name(partition):
        return jsonify({'error': 'Invalid partition name'}), 400
    if not _dev_exists(partition):
        return jsonify({'error': f'/dev/{partition} not found'}), 404

    # Detect filesystem type
    r = host_run(f"sudo /opt/ethos/tools/ethos-system-helper.sh lsblk -nlo FSTYPE /dev/{partition} 2>/dev/null", timeout=5)
    fstype = r.stdout.strip() if r.returncode == 0 else ''

    result = {
        'partition': partition,
        'fstype': fstype,
        'details': {},
    }

    if fstype in ('ext2', 'ext3', 'ext4'):
        tr = host_run(f"tune2fs -l /dev/{partition} 2>/dev/null", timeout=10)
        if tr.returncode == 0 and tr.stdout:
            details = {}
            for line in tr.stdout.strip().splitlines():
                if ':' in line:
                    key, _, val = line.partition(':')
                    details[key.strip()] = val.strip()
            result['details'] = {
                'filesystem_state': details.get('Filesystem state', ''),
                'block_count': details.get('Block count', ''),
                'free_blocks': details.get('Free blocks', ''),
                'block_size': details.get('Block size', ''),
                'inode_count': details.get('Inode count', ''),
                'free_inodes': details.get('Free inodes', ''),
                'last_checked': details.get('Last checked', ''),
                'mount_count': details.get('Mount count', ''),
                'max_mount_count': details.get('Maximum mount count', ''),
                'errors_behavior': details.get('Errors behavior', ''),
                'filesystem_created': details.get('Filesystem created', ''),
                'last_mounted': details.get('Last mounted on', ''),
                'filesystem_uuid': details.get('Filesystem UUID', ''),
                'volume_name': details.get('Filesystem volume name', ''),
            }
    elif fstype == 'ntfs':
        nr = host_run(f"ntfsinfo -m /dev/{partition} 2>/dev/null", timeout=10)
        if nr.returncode == 0 and nr.stdout:
            result['details']['raw'] = nr.stdout.strip()
        else:
            # fallback
            nr2 = host_run(f"ntfsinfo /dev/{partition} 2>/dev/null", timeout=10)
            if nr2.returncode == 0 and nr2.stdout:
                result['details']['raw'] = nr2.stdout.strip()
    elif fstype in ('xfs',):
        xr = host_run(f"xfs_info /dev/{partition} 2>/dev/null", timeout=10)
        if xr.returncode == 0 and xr.stdout:
            result['details']['raw'] = xr.stdout.strip()
    elif fstype in ('btrfs',):
        br = host_run(f"btrfs filesystem show /dev/{partition} 2>/dev/null", timeout=10)
        if br.returncode == 0 and br.stdout:
            result['details']['raw'] = br.stdout.strip()
    else:
        result['details']['note'] = f'No detailed info available for filesystem type: {fstype or "unknown"}'

    return jsonify(result)


# ── Package: install / uninstall / status ──

def _diskrepair_on_uninstall(wipe):
    """Kill active scan/repair process on uninstall."""
    with _repair_lock:
        if _repair_state['status'] == 'running':
            pid = _repair_state.get('pid', 0)
            if pid and pid > 0:
                try:
                    os.kill(pid, signal.SIGTERM)
                    time.sleep(1)
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                except OSError:
                    pass
    _reset_state()


register_pkg_routes(
    diskrepair_bp,
    install_message='Disk repair ready.',
    install_deps=['smartctl'],
    wipe_files=[_REPAIR_STATE_FILE, _HISTORY_FILE],
    on_uninstall=_diskrepair_on_uninstall,
)
