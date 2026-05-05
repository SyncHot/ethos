"""EthOS Storage — Health, SMART & Maintenance Routes

Routes: /api/storage/health, /api/storage/smart/*, /api/storage/maintenance/*,
        /api/storage/app-usage, /api/storage/clean-tmp,
        /api/storage/network/*
"""
import json
import os
import re
import sys
import time
import logging
import sqlite3
import base64
from flask import jsonify, request, Response, stream_with_context

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import data_path, q as _q_imported, get_data_disk as _get_data_disk
from host import host_run as _host_run_base
from utils import check_tool
from blueprints.admin_required import admin_required
from blueprints.storage import (
    storage_bp,
    host_run, Q,
    _invalidate_drives_cache,
)


def _get_sio():
    """Get the current SocketIO instance from the parent storage module."""
    m = sys.modules.get('blueprints.storage')
    return m._socketio if m else None


logger = logging.getLogger(__name__)

# ── Storage Health Monitor ─────────────────────────────────────────────

_HEALTH_FILE = data_path('storage_health.json')
_HEALTH_INTERVAL = 300  # 5 minutes


def _health_monitor_loop():
    """Periodic storage health check — SMART, RAID, disk usage."""
    import gevent
    gevent.sleep(60)  # Wait for system to stabilize after boot
    logger = logging.getLogger('storage')

    while True:
        try:
            alerts = []
            now = time.strftime('%Y-%m-%dT%H:%M:%S')

            # 1) SMART health check — all non-removable disks
            r = _host_run_base("lsblk -dn -o NAME,TRAN,TYPE 2>/dev/null")
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    parts = line.split()
                    if len(parts) < 3:
                        continue
                    dname, tran, dtype = parts[0], parts[1] if len(parts) > 1 else '', parts[-1]
                    if dtype != 'disk' or tran == 'usb':
                        continue
                    sr = _host_run_base(f"smartctl -H /dev/{_q_imported(dname)} 2>/dev/null", timeout=15)
                    if 'FAILED' in (sr.stdout or ''):
                        alerts.append({'type': 'smart', 'level': 'error', 'disk': dname,
                                       'message': f'SMART health FAILED on /dev/{dname}'})
                    elif sr.returncode not in (0, 4) and 'PASSED' not in (sr.stdout or ''):
                        pass  # SMART not available, skip

            # 2) RAID status check
            r = _host_run_base("cat /proc/mdstat 2>/dev/null")
            if r.returncode == 0 and r.stdout:
                current_md = None
                for line in r.stdout.splitlines():
                    m = re.match(r'^(md\d+)\s*:', line)
                    if m:
                        current_md = m.group(1)
                    if current_md and '_' in line:
                        bm = re.search(r'\[([U_]+)\]', line)
                        if bm and '_' in bm.group(1):
                            alerts.append({'type': 'raid', 'level': 'error', 'array': current_md,
                                           'message': f'RAID array /dev/{current_md} is DEGRADED ({bm.group(1)})'})
                            current_md = None

            # 3) Disk usage check (>90%)
            r = _host_run_base("df -B1 --output=target,pcent 2>/dev/null | tail -n +2")
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.rsplit(None, 1)
                    if len(parts) < 2:
                        continue
                    mount, pct = parts[0].strip(), parts[1].replace('%', '').strip()
                    try:
                        pval = int(pct)
                    except ValueError:
                        continue
                    if pval >= 90 and not mount.startswith(('/snap', '/run', '/sys', '/proc', '/dev')):
                        alerts.append({'type': 'disk_usage', 'level': 'warning' if pval < 95 else 'error',
                                       'mount': mount, 'percent': pval,
                                       'message': f'Disk usage at {pval}% on {mount}'})

            # Save health state
            health = {'last_check': now, 'alerts': alerts, 'ok': len(alerts) == 0}
            try:
                with open(_HEALTH_FILE, 'w') as f:
                    json.dump(health, f, indent=2)
            except Exception as e:
                logger.warning('Failed to write health file: %s', e)

            # Send notifications for new alerts
            if alerts:
                try:
                    from blueprints.notifications import send_notification, push_inbox
                    for a in alerts:
                        cat = 'smart' if a['type'] == 'smart' else 'raid' if a['type'] == 'raid' else 'storage'
                        lvl = a.get('level', 'warning')
                        send_notification('\u26a0\ufe0f Storage Alert', a['message'], category=cat, level=lvl)
                        push_inbox('Storage Alert', a['message'], msg_type=lvl, category='storage',
                                   action_app='storage-manager', action_tab='diagnostics')
                except Exception as e:
                    logger.warning('Failed to send health notification: %s', e)

            # Emit via SocketIO for live dashboard
            _sio = _get_sio()
            if _sio:
                _sio.emit('storage_health', health)

        except Exception as e:
            logger.warning('Health monitor error: %s', e)

        import gevent
        gevent.sleep(_HEALTH_INTERVAL)


@storage_bp.route('/health')
@admin_required
def storage_health():
    """Get current storage health status."""
    try:
        with open(_HEALTH_FILE) as f:
            return jsonify(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return jsonify({'last_check': None, 'alerts': [], 'ok': True})


# ── SMART Test Scheduling ──────────────────────────────────────────────

_SMART_SCHEDULE_FILE = data_path('smart_schedule.json')


def _load_smart_schedule():
    try:
        with open(_SMART_SCHEDULE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {'tests': []}


def _save_smart_schedule(data):
    os.makedirs(os.path.dirname(_SMART_SCHEDULE_FILE), exist_ok=True)
    with open(_SMART_SCHEDULE_FILE, 'w') as f:
        json.dump(data, f, indent=2)


@storage_bp.route('/smart/test', methods=['POST'])
@admin_required
def smart_test_trigger():
    """Trigger a SMART self-test on a disk."""
    data = request.get_json(force=True)
    disk = data.get('disk', '').strip()
    test_type = data.get('type', 'short')  # short, long, conveyance

    if not disk or not re.match(r'^[a-zA-Z0-9]+$', disk):
        return jsonify({'error': 'Invalid disk name'}), 400
    if test_type not in ('short', 'long', 'conveyance'):
        return jsonify({'error': 'Invalid test type (short/long/conveyance)'}), 400

    r = _host_run_base(f"smartctl -t {_q_imported(test_type)} /dev/{_q_imported(disk)} 2>&1", timeout=15)

    # Parse expected completion time from output
    est_minutes = None
    for line in (r.stdout or '').splitlines():
        m = re.search(r'Please wait (\d+) minutes', line)
        if m:
            est_minutes = int(m.group(1))
            break

    if r.returncode in (0, 4):
        return jsonify({'ok': True, 'message': f'{test_type} test started on /dev/{disk}',
                        'estimated_minutes': est_minutes})
    else:
        return jsonify({'error': f'Failed to start test: {(r.stdout or r.stderr or "unknown error")[:200]}'}), 500


@storage_bp.route('/smart/test/result')
@admin_required
def smart_test_result():
    """Get SMART self-test log for a disk."""
    disk = request.args.get('disk', '').strip()
    if not disk or not re.match(r'^[a-zA-Z0-9]+$', disk):
        return jsonify({'error': 'Invalid disk name'}), 400

    r = _host_run_base(f"smartctl -l selftest /dev/{_q_imported(disk)} 2>&1", timeout=15)

    tests = []
    if r.returncode in (0, 4) and r.stdout:
        in_table = False
        for line in r.stdout.splitlines():
            if 'Num' in line and 'Test_Description' in line:
                in_table = True
                continue
            if in_table and line.strip():
                parts = line.split()
                if len(parts) >= 5 and parts[0].startswith('#'):
                    test_entry = {
                        'num': parts[0],
                        'type': parts[1] if len(parts) > 1 else '',
                        'status': ' '.join(parts[2:-2]) if len(parts) > 4 else parts[2] if len(parts) > 2 else '',
                        'remaining': parts[-2] if len(parts) > 3 else '',
                        'lifetime_hours': parts[-1] if len(parts) > 4 else '',
                    }
                    tests.append(test_entry)

    # Check if a test is currently running
    running = False
    progress = None
    r2 = _host_run_base(f"smartctl -c /dev/{_q_imported(disk)} 2>&1", timeout=10)
    if r2.returncode in (0, 4) and r2.stdout:
        for line in r2.stdout.splitlines():
            if 'Self-test execution status' in line and 'progress' in line.lower():
                running = True
                m = re.search(r'(\d+)%', line)
                if m:
                    progress = 100 - int(m.group(1))  # smartctl shows remaining %, we want completed %

    return jsonify({'tests': tests, 'running': running, 'progress': progress})


@storage_bp.route('/smart/schedule', methods=['GET'])
@admin_required
def smart_schedule_list():
    """Get all scheduled SMART tests."""
    return jsonify(_load_smart_schedule())


@storage_bp.route('/smart/schedule', methods=['POST'])
@admin_required
def smart_schedule_add():
    """Add a scheduled SMART test."""
    data = request.get_json(force=True)
    disk = data.get('disk', '').strip()
    test_type = data.get('type', 'short')
    frequency = data.get('frequency', 'weekly')  # daily, weekly, monthly

    if not disk or not re.match(r'^[a-zA-Z0-9]+$', disk):
        return jsonify({'error': 'Invalid disk name'}), 400
    if test_type not in ('short', 'long', 'conveyance'):
        return jsonify({'error': 'Invalid test type'}), 400
    if frequency not in ('daily', 'weekly', 'monthly'):
        return jsonify({'error': 'Invalid frequency'}), 400

    cron_map = {
        'daily': {'minute': '0', 'hour': '3', 'dom': '*', 'month': '*', 'dow': '*'},
        'weekly': {'minute': '0', 'hour': '3', 'dom': '*', 'month': '*', 'dow': '0'},
        'monthly': {'minute': '0', 'hour': '3', 'dom': '1', 'month': '*', 'dow': '*'},
    }
    cron = cron_map[frequency]

    schedule = _load_smart_schedule()

    # Check for duplicate
    for t in schedule.get('tests', []):
        if t['disk'] == disk and t['type'] == test_type:
            return jsonify({'error': f'Test already scheduled for {disk}'}), 409

    entry = {
        'id': f'{disk}_{test_type}_{int(time.time())}',
        'disk': disk,
        'type': test_type,
        'frequency': frequency,
        'cron': cron,
        'enabled': True,
        'created': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'last_run': None,
        'last_result': None,
    }
    schedule.setdefault('tests', []).append(entry)
    _save_smart_schedule(schedule)

    # Register cron job
    cmd = f"smartctl -t {test_type} /dev/{disk}"
    _register_cron_job(cron, cmd, f'SMART {test_type} test on {disk}')

    return jsonify({'ok': True, 'test': entry})


@storage_bp.route('/smart/schedule/<test_id>', methods=['DELETE'])
@admin_required
def smart_schedule_delete(test_id):
    """Remove a scheduled SMART test."""
    schedule = _load_smart_schedule()
    tests = schedule.get('tests', [])
    target = None
    for t in tests:
        if t.get('id') == test_id:
            target = t
            break

    if not target:
        return jsonify({'error': 'Scheduled test not found'}), 404

    # Remove cron job
    cmd = f"smartctl -t {target['type']} /dev/{target['disk']}"
    _unregister_cron_job(cmd)

    schedule['tests'] = [t for t in tests if t.get('id') != test_id]
    _save_smart_schedule(schedule)
    return jsonify({'ok': True})


def _register_cron_job(cron, command, description):
    """Add a cron job to root crontab. Idempotent."""
    r = _host_run_base("sudo -n crontab -l 2>/dev/null || true")
    existing = r.stdout or ''
    if command in existing:
        return  # Already exists
    line = f"# DESC: {description}\n{cron['minute']} {cron['hour']} {cron['dom']} {cron['month']} {cron['dow']} {command}\n"
    new_crontab = existing.rstrip('\n') + '\n' + line
    _host_run_base(f"echo {_q_imported(new_crontab)} | sudo -n crontab -")


def _unregister_cron_job(command):
    """Remove a cron job from root crontab by command match."""
    r = _host_run_base("sudo -n crontab -l 2>/dev/null || true")
    if not r.stdout:
        return
    lines = r.stdout.splitlines()
    filtered = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if command in line:
            continue
        if line.startswith('# DESC:'):
            idx = lines.index(line)
            if idx + 1 < len(lines) and command in lines[idx + 1]:
                skip_next = True
                continue
        filtered.append(line)
    new_crontab = '\n'.join(filtered) + '\n'
    _host_run_base(f"echo {_q_imported(new_crontab)} | sudo -n crontab -")


# ── Storage Maintenance ────────────────────────────────────────────────

_MAINT_DB = os.path.join(os.environ.get('ETHOS_LOG_DIR', '/opt/ethos/logs'), 'maintenance.db')
_active_maintenance = {}  # {task_id: {type, target, started, pid}}


def _init_maint_db():
    """Initialize maintenance history SQLite database."""
    import sqlite3
    conn = sqlite3.connect(_MAINT_DB)
    conn.execute('''CREATE TABLE IF NOT EXISTS maintenance_history (
        id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        target TEXT NOT NULL,
        started TEXT NOT NULL,
        finished TEXT,
        status TEXT DEFAULT 'running',
        result TEXT,
        duration_sec REAL
    )''')
    conn.commit()
    conn.close()


_init_maint_db()


@storage_bp.route('/maintenance/start', methods=['POST'])
@admin_required
def maintenance_start():
    """Start a maintenance task: scrub, raid-check, or trim."""
    data = request.get_json(force=True)
    mtype = data.get('type', '').strip()
    target = data.get('target', '').strip()

    if mtype not in ('scrub', 'raid-check', 'trim'):
        return jsonify({'error': 'Invalid type (scrub/raid-check/trim)'}), 400
    if not target:
        return jsonify({'error': 'Target required'}), 400

    # Validate target based on type
    if mtype == 'scrub':
        if not target.startswith('/') or '..' in target:
            return jsonify({'error': 'Invalid mount path'}), 400
        r = _host_run_base(f"stat -f -c %T {_q_imported(target)} 2>/dev/null")
        if 'btrfs' not in (r.stdout or '').lower():
            return jsonify({'error': f'{target} is not a btrfs filesystem'}), 400
        cmd = f"btrfs scrub start -B {_q_imported(target)}"

    elif mtype == 'raid-check':
        if not re.match(r'^md\d+$', target):
            return jsonify({'error': 'Invalid RAID device name'}), 400
        cmd = f"echo check | sudo -n tee /sys/block/{_q_imported(target)}/md/sync_action"

    elif mtype == 'trim':
        if not target.startswith('/') or '..' in target:
            return jsonify({'error': 'Invalid mount path'}), 400
        cmd = f"fstrim -v {_q_imported(target)}"

    # Check for duplicate running task
    for tid, task in _active_maintenance.items():
        if task['type'] == mtype and task['target'] == target and task['status'] == 'running':
            return jsonify({'error': f'{mtype} already running on {target}'}), 409

    task_id = f"{mtype}_{int(time.time())}"
    started = time.strftime('%Y-%m-%dT%H:%M:%S')

    # Record in DB
    import sqlite3
    conn = sqlite3.connect(_MAINT_DB)
    conn.execute('INSERT INTO maintenance_history (id, type, target, started) VALUES (?, ?, ?, ?)',
                 (task_id, mtype, target, started))
    conn.commit()
    conn.close()

    _active_maintenance[task_id] = {'type': mtype, 'target': target, 'started': started, 'status': 'running'}

    # Run in background
    def _run_task():
        logger = logging.getLogger('storage')
        result_text = ''
        status = 'completed'
        try:
            r = _host_run_base(cmd, timeout=7200)  # 2 hour timeout for scrub
            result_text = (r.stdout or '') + (r.stderr or '')
            if r.returncode != 0:
                status = 'failed'
                result_text = f'Exit code {r.returncode}: {result_text}'
        except Exception as e:
            status = 'failed'
            result_text = str(e)

        finished = time.strftime('%Y-%m-%dT%H:%M:%S')
        duration = time.time() - time.mktime(time.strptime(started, '%Y-%m-%dT%H:%M:%S'))

        try:
            conn2 = sqlite3.connect(_MAINT_DB)
            conn2.execute('UPDATE maintenance_history SET finished=?, status=?, result=?, duration_sec=? WHERE id=?',
                          (finished, status, result_text[:2000], duration, task_id))
            conn2.commit()
            conn2.close()
        except Exception as e2:
            logger.warning('Failed to update maintenance record: %s', e2)

        _active_maintenance[task_id]['status'] = status
        _active_maintenance[task_id]['finished'] = finished
        _active_maintenance[task_id]['result'] = result_text[:500]

        # Notify on failure
        if status == 'failed':
            try:
                from blueprints.notifications import send_notification, push_inbox
                send_notification('\u26a0\ufe0f Maintenance Failed', f'{mtype} on {target} failed: {result_text[:200]}',
                                  category='storage', level='warning')
                push_inbox('Maintenance Failed', f'{mtype} on {target} failed', msg_type='warning', category='storage')
            except Exception:
                pass

        _sio = _get_sio()
        if _sio:
            _sio.emit('maintenance_complete', {'task_id': task_id, 'status': status, 'type': mtype, 'target': target})

    _sio2 = _get_sio()
    if _sio2:
        _sio2.start_background_task(_run_task)
    else:
        import gevent
        gevent.spawn(_run_task)

    return jsonify({'ok': True, 'task_id': task_id, 'message': f'{mtype} started on {target}'})


@storage_bp.route('/maintenance/status')
@admin_required
def maintenance_status():
    """Get status of active maintenance tasks + RAID sync progress."""
    tasks = []
    for tid, task in list(_active_maintenance.items()):
        entry = {**task, 'id': tid}

        # For raid-check, get sync progress from /proc/mdstat
        if task['type'] == 'raid-check' and task['status'] == 'running':
            r = _host_run_base(f"cat /sys/block/{_q_imported(task['target'])}/md/sync_completed 2>/dev/null")
            if r.returncode == 0 and '/' in (r.stdout or ''):
                parts = r.stdout.strip().split('/')
                try:
                    done, total = int(parts[0].strip()), int(parts[1].strip())
                    entry['progress'] = round(done / total * 100, 1) if total > 0 else 0
                except (ValueError, ZeroDivisionError):
                    pass
            # Check if still running
            r2 = _host_run_base(f"cat /sys/block/{_q_imported(task['target'])}/md/sync_action 2>/dev/null")
            if (r2.stdout or '').strip() == 'idle':
                task['status'] = 'completed'
                entry['status'] = 'completed'

        # For scrub, get live progress from btrfs scrub status
        if task['type'] == 'scrub' and task['status'] == 'running':
            r = _host_run_base(f"btrfs scrub status {_q_imported(task['target'])} 2>/dev/null", timeout=5)
            if r.returncode == 0:
                info = _parse_scrub_status(r.stdout or '')
                if info['pct'] > 0:
                    entry['progress'] = info['pct']
                if info['rate']:
                    entry['rate'] = info['rate']

        tasks.append(entry)

    return jsonify({'tasks': tasks})


@storage_bp.route('/maintenance/history')
@admin_required
def maintenance_history():
    """Get maintenance history from SQLite."""
    limit = request.args.get('limit', 50, type=int)
    import sqlite3
    conn = sqlite3.connect(_MAINT_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM maintenance_history ORDER BY started DESC LIMIT ?', (limit,)).fetchall()
    conn.close()
    return jsonify({'history': [dict(r) for r in rows]})


@storage_bp.route('/maintenance/schedule', methods=['POST'])
@admin_required
def maintenance_schedule():
    """Schedule a recurring maintenance task via cron."""
    data = request.get_json(force=True)
    mtype = data.get('type', '').strip()
    target = data.get('target', '').strip()
    frequency = data.get('frequency', 'weekly')

    if mtype not in ('scrub', 'raid-check', 'trim'):
        return jsonify({'error': 'Invalid type'}), 400
    if not target:
        return jsonify({'error': 'Target required'}), 400
    if frequency not in ('daily', 'weekly', 'monthly'):
        return jsonify({'error': 'Invalid frequency'}), 400

    cron_map = {
        'daily': {'minute': '0', 'hour': '4', 'dom': '*', 'month': '*', 'dow': '*'},
        'weekly': {'minute': '0', 'hour': '4', 'dom': '*', 'month': '*', 'dow': '0'},
        'monthly': {'minute': '0', 'hour': '4', 'dom': '1', 'month': '*', 'dow': '*'},
    }
    cron = cron_map[frequency]

    if mtype == 'scrub':
        cmd = f"btrfs scrub start {target}"
    elif mtype == 'raid-check':
        cmd = f"echo check > /sys/block/{target}/md/sync_action"
    elif mtype == 'trim':
        cmd = f"fstrim {target}"

    _register_cron_job(cron, cmd, f'Storage maintenance: {mtype} on {target}')
    return jsonify({'ok': True, 'message': f'{mtype} scheduled {frequency} on {target}'})


@storage_bp.route('/maintenance/schedules')
@admin_required
def maintenance_schedules():
    """List active maintenance cron schedules."""
    r = _host_run_base("sudo -n crontab -l 2>/dev/null || true")
    if not r.stdout:
        return jsonify({'schedules': []})

    schedules = []
    lines = r.stdout.splitlines()
    for i, line in enumerate(lines):
        if 'Storage maintenance:' not in line and not (i + 1 < len(lines) and 'Storage maintenance:' in lines[i]):
            continue
        if line.startswith('# DESC: Storage maintenance:'):
            if i + 1 < len(lines):
                cron_line = lines[i + 1]
                desc = line.replace('# DESC: Storage maintenance: ', '')
                parts = cron_line.split(None, 5)
                if len(parts) >= 6:
                    minute, hour, dom, month, dow = parts[:5]
                    cmd = parts[5]
                    # Determine frequency
                    if dom == '1' and dow == '*':
                        freq = 'monthly'
                    elif dow in ('0', '7') and dom == '*':
                        freq = 'weekly'
                    else:
                        freq = 'daily'
                    # Determine type and target from desc or command
                    mtype = 'scrub' if 'scrub' in desc else 'raid-check' if 'raid' in desc else 'trim'
                    target = desc.split(' on ')[-1] if ' on ' in desc else ''
                    schedules.append({
                        'type': mtype, 'target': target, 'frequency': freq,
                        'cron': f'{minute} {hour} {dom} {month} {dow}',
                        'command': cmd,
                    })
    return jsonify({'schedules': schedules})


@storage_bp.route('/maintenance/schedule', methods=['DELETE'])
@admin_required
def maintenance_schedule_delete():
    """Remove a maintenance schedule."""
    data = request.get_json(force=True)
    mtype = data.get('type', '').strip()
    target = data.get('target', '').strip()
    if not mtype or not target:
        return jsonify({'error': 'type and target required'}), 400

    if mtype == 'scrub':
        cmd = f"btrfs scrub start {target}"
    elif mtype == 'raid-check':
        cmd = f"echo check > /sys/block/{target}/md/sync_action"
    elif mtype == 'trim':
        cmd = f"fstrim {target}"
    else:
        return jsonify({'error': 'Invalid type'}), 400

    _unregister_cron_job(cmd)
    return jsonify({'ok': True, 'message': f'Schedule removed for {mtype} on {target}'})


def _parse_scrub_status(output):
    """Parse btrfs scrub status output into a dict."""
    info = {'status': 'unknown', 'errors': None, 'started': None,
            'duration': None, 'total_bytes': 0, 'scrubbed_bytes': 0, 'rate': None, 'pct': 0}
    if not output:
        return info
    for line in output.splitlines():
        line = line.strip()
        if line.startswith('Scrub started:'):
            info['started'] = line.split(':', 1)[1].strip()
        elif line.startswith('Status:'):
            info['status'] = line.split(':', 1)[1].strip()
        elif line.startswith('Duration:'):
            info['duration'] = line.split(':', 1)[1].strip()
        elif line.startswith('Total to scrub:'):
            raw = line.split(':', 1)[1].strip()
            info['total_str'] = raw
            info['total_bytes'] = _parse_size_to_bytes(raw)
        elif line.startswith('Bytes scrubbed:'):
            raw = line.split(':', 1)[1].strip()
            m = re.match(r'([\d.]+\s*\S+)', raw)
            if m:
                info['scrubbed_bytes'] = _parse_size_to_bytes(m.group(1))
            pct_m = re.search(r'\(([\d.]+)%\)', raw)
            if pct_m:
                info['pct'] = float(pct_m.group(1))
        elif line.startswith('Rate:'):
            info['rate'] = line.split(':', 1)[1].strip()
        elif line.startswith('Error summary:'):
            info['errors'] = line.split(':', 1)[1].strip()
        elif 'no errors found' in line.lower():
            info['errors'] = 'no errors found'
    # Calculate pct from bytes if not parsed
    if info['pct'] == 0 and info['total_bytes'] > 0 and info['scrubbed_bytes'] > 0:
        info['pct'] = round(info['scrubbed_bytes'] / info['total_bytes'] * 100, 1)
    # Finished scrub = 100%
    if info['status'] == 'finished':
        info['pct'] = 100
    return info


def _parse_size_to_bytes(s):
    """Parse '107.38GiB' or '42.15MiB' etc. to bytes."""
    s = s.strip()
    m = re.match(r'([\d.]+)\s*(GiB|MiB|KiB|TiB|GB|MB|KB|TB|B)', s, re.IGNORECASE)
    if not m:
        return 0
    val = float(m.group(1))
    unit = m.group(2).lower()
    multipliers = {'b': 1, 'kib': 1024, 'kb': 1000, 'mib': 1024**2, 'mb': 10**6,
                   'gib': 1024**3, 'gb': 10**9, 'tib': 1024**4, 'tb': 10**12}
    return int(val * multipliers.get(unit, 1))


@storage_bp.route('/maintenance/scrub-info')
@admin_required
def maintenance_scrub_info():
    """Get btrfs scrub status for all mounted btrfs pools."""
    # Get btrfs mount points from findmnt (reliable)
    r = _host_run_base("findmnt -t btrfs -n -o TARGET 2>/dev/null")
    mounts = [p.strip() for p in (r.stdout or '').splitlines() if p.strip()]

    results = []
    for mount in mounts:
        r = _host_run_base(f"btrfs scrub status {_q_imported(mount)} 2>/dev/null", timeout=10)
        info = _parse_scrub_status(r.stdout or '')
        info['mount'] = mount
        info['name'] = os.path.basename(mount) or mount
        results.append(info)

    return jsonify({'pools': results})


@storage_bp.route('/app-usage')
@admin_required
def app_usage():
    """Return disk usage per known app directory on root and data partition.
    Used by Storage Manager to show what is consuming space."""
    results = []

    def _du(path, label, partition):
        if not os.path.isdir(path) and not os.path.isfile(path):
            return None
        r = _host_run_base(f'du -sb {_q_imported(path)} 2>/dev/null | cut -f1', timeout=15)
        try:
            size = int(r.stdout.strip())
        except (ValueError, AttributeError):
            return None
        return {'label': label, 'path': path, 'bytes': size, 'partition': partition}

    # Root partition consumers
    root_apps = [
        ('/var/lib/docker',      'Docker (dane)',        '/'),
        ('/var/lib/clamav',      'ClamAV DB',            '/'),
        ('/var/lib/minidlna',    'MiniDLNA DB',          '/'),
        ('/var/lib/apt',         'APT cache/lists',      '/'),
        ('/var/cache/apt',       'APT packages cache',   '/'),
        ('/var/log',             'Logi systemowe',       '/'),
        ('/tmp',                 'Pliki tymczasowe /tmp', '/'),
        ('/opt/ethos/venv',      'Python venv',          '/'),
    ]
    for path, label, part in root_apps:
        item = _du(path, label, part)
        if item:
            results.append(item)

    # Data partition consumers
    dd = _get_data_disk()
    if dd:
        data_apps = [
            (os.path.join(dd, 'docker'),    'Docker (dane)',    dd),
            (os.path.join(dd, 'clamav'),    'ClamAV DB',        dd),
            (os.path.join(dd, 'minidlna'),  'MiniDLNA DB',      dd),
            (os.path.join(dd, 'vms'),       'VM Manager',       dd),
            (os.path.join(dd, 'ethos', 'data', 'models'),    'Modele AI',        dd),
            (os.path.join(dd, 'ethos', 'data', 'surveillance'), 'Nadzór / nagrania', dd),
            (os.path.join(dd, 'ethos', 'data', 'video_thumbs'),  'Video miniatury', dd),
            (os.path.join(dd, 'ethos', 'backups'),  'Kopie zapasowe',   dd),
            (os.path.join(dd, 'ethos', 'uploads'),  'Przesłane pliki',  dd),
        ]
        for path, label, part in data_apps:
            item = _du(path, label, part)
            if item:
                results.append(item)

    # Sort by size descending
    results.sort(key=lambda x: x['bytes'], reverse=True)
    return jsonify({'items': results})


# ---------------------------------------------------------------------------
# /tmp cleanup
# ---------------------------------------------------------------------------

@storage_bp.route('/clean-tmp', methods=['GET'])
@admin_required
def clean_tmp_info():
    """Return /tmp usage stats: total, used, free, file count, and list of top items."""
    import shutil
    usage = shutil.disk_usage('/tmp')
    items = []
    try:
        for name in os.listdir('/tmp'):
            p = os.path.join('/tmp', name)
            try:
                r = _host_run_base(f'du -sb {_q_imported(p)} 2>/dev/null | cut -f1', timeout=10)
                size = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0
                mtime = os.path.getmtime(p)
                items.append({'name': name, 'bytes': size, 'mtime': mtime, 'is_dir': os.path.isdir(p)})
            except (OSError, ValueError):
                pass
    except OSError:
        pass
    items.sort(key=lambda x: x['bytes'], reverse=True)
    return jsonify({
        'ok': True,
        'total': usage.total,
        'used': usage.used,
        'free': usage.free,
        'pct': round(usage.used * 100 / usage.total) if usage.total else 0,
        'items': items[:50],
    })


@storage_bp.route('/clean-tmp', methods=['POST'])
@admin_required
def clean_tmp():
    """Clean /tmp files older than specified max_age_minutes (default 60).
    Skips system sockets (.X11-unix, .ICE-unix, etc.) and systemd private dirs."""
    import shutil as _shutil
    data = request.get_json(force=True, silent=True) if request.is_json else {}
    if not isinstance(data, dict):
        data = {}
    max_age = max(0, int(data.get('max_age_minutes', 60)))
    cutoff = time.time() - max_age * 60

    # Protected prefixes — never remove these
    protected = {'.X11-unix', '.ICE-unix', '.XIM-unix', '.font-unix'}

    before_free = _shutil.disk_usage('/tmp').free
    removed = 0
    errors = []

    try:
        for name in os.listdir('/tmp'):
            if name in protected or name.startswith('systemd-private-'):
                continue
            p = os.path.join('/tmp', name)
            try:
                mtime = os.path.getmtime(p)
                if mtime >= cutoff:
                    continue
                if os.path.isdir(p):
                    _shutil.rmtree(p, ignore_errors=True)
                else:
                    os.unlink(p)
                removed += 1
            except OSError as e:
                errors.append(f'{name}: {e}')
    except OSError as e:
        return jsonify({'error': f'Cannot read /tmp: {e}'}), 500

    after_free = _shutil.disk_usage('/tmp').free
    freed = after_free - before_free

    try:
        from blueprints.eventlog import elog
        elog('storage', 'info', f'/tmp cleanup: removed {removed} items, freed {freed // (1024*1024)} MB')
    except Exception:
        pass

    return jsonify({
        'ok': True,
        'removed': removed,
        'freed': freed,
        'errors': errors[:10],
        'free_after': after_free,
    })


# ---------------------------------------------------------------------------
# Network Drive Mounts (SMB / NFS / WebDAV client-side mounts)
# ---------------------------------------------------------------------------

_NETMOUNT_DIR = '/mnt/network'
_NETMOUNT_CONF = data_path('network_mounts.json')


def _load_network_mounts():
    """Load saved network mount configurations."""
    try:
        with open(_NETMOUNT_CONF, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save_network_mounts(mounts):
    """Persist network mount configurations."""
    _host_write_json(_NETMOUNT_CONF, mounts)


def _netmount_path(mount_id):
    """Return the mount point path for a given network mount ID."""
    return os.path.join(_NETMOUNT_DIR, mount_id)


def _is_mounted(path):
    """Check if a path is currently a mount point."""
    r = host_run(f"findmnt -n -o TARGET {Q(path)} 2>/dev/null", timeout=5)
    return r.returncode == 0 and path in r.stdout.strip()


def _mount_smb(cfg):
    """Mount an SMB/CIFS share. Returns (ok, error_msg)."""
    mp = _netmount_path(cfg['id'])
    host_run(f"mkdir -p {Q(mp)}")

    host_addr = cfg['host']
    share = cfg['share']
    username = cfg.get('username', 'guest')
    password = cfg.get('password', '')
    domain = cfg.get('domain', '')

    opts = ['rw', 'iocharset=utf8', 'file_mode=0777', 'dir_mode=0777', 'noperm']
    if username and username.lower() != 'guest':
        opts.append(f'username={username}')
        if password:
            opts.append(f'password={password}')
        if domain:
            opts.append(f'domain={domain}')
    else:
        opts.extend(['guest', 'username=guest'])

    # vers=3.0 first, fallback to auto
    for ver in ('3.0', '2.1', '1.0'):
        cmd = f"mount -t cifs //{Q(host_addr)}/{Q(share)} {Q(mp)} -o {','.join(opts)},vers={ver}"
        r = host_run(cmd, timeout=15)
        if r.returncode == 0:
            return True, ''
    return False, r.stderr.strip() if r else 'Mount failed'


def _mount_nfs(cfg):
    """Mount an NFS share. Returns (ok, error_msg)."""
    mp = _netmount_path(cfg['id'])
    host_run(f"mkdir -p {Q(mp)}")

    host_addr = cfg['host']
    export_path = cfg['share']

    opts = 'rw,soft,timeo=30,retrans=3'
    cmd = f"mount -t nfs {Q(host_addr)}:{Q(export_path)} {Q(mp)} -o {opts}"
    r = host_run(cmd, timeout=15)
    if r.returncode == 0:
        return True, ''
    # Fallback: nfs4
    cmd = f"mount -t nfs4 {Q(host_addr)}:{Q(export_path)} {Q(mp)} -o {opts}"
    r = host_run(cmd, timeout=15)
    if r.returncode == 0:
        return True, ''
    return False, r.stderr.strip()


@storage_bp.route('/network/mounts')
@admin_required
def network_mount_list():
    """List all saved network mounts with their current status."""
    mounts = _load_network_mounts()
    result = []
    for m in mounts:
        mp = _netmount_path(m['id'])
        mounted = _is_mounted(mp)
        usage = None
        if mounted:
            r = host_run(f"df -B1 {Q(mp)} 2>/dev/null | tail -1", timeout=5)
            parts = r.stdout.split()
            if len(parts) >= 5:
                try:
                    total = int(parts[1])
                    used = int(parts[2])
                    usage = {
                        'total': total,
                        'used': used,
                        'percent': round(used / total * 100, 1) if total > 0 else 0,
                    }
                except (ValueError, IndexError):
                    pass
        result.append({
            'id': m['id'],
            'name': m.get('name', ''),
            'protocol': m.get('protocol', 'smb'),
            'host': m.get('host', ''),
            'share': m.get('share', ''),
            'mount_path': mp,
            'mounted': mounted,
            'auto_mount': m.get('auto_mount', False),
            'usage': usage,
        })
    return jsonify({'mounts': result})


@storage_bp.route('/network/mount', methods=['POST'])
@admin_required
def network_mount_add():
    """Add and mount a new network drive."""
    data = request.json or {}
    if not isinstance(data, dict):
        data = {}
    protocol = data.get('protocol', 'smb').lower()
    host_addr = data.get('host', '').strip()
    share = data.get('share', '').strip().strip('/')
    name = data.get('name', '').strip()
    username = data.get('username', '').strip()
    password = data.get('password', '')
    domain = data.get('domain', '').strip()
    auto_mount = bool(data.get('auto_mount', True))

    if not host_addr:
        return jsonify({'error': 'Host address is required'}), 400
    if not share:
        return jsonify({'error': 'Share name / export path is required'}), 400
    if protocol not in ('smb', 'nfs'):
        return jsonify({'error': 'Protocol must be smb or nfs'}), 400

    # Ensure cifs-utils or nfs-common is installed
    if protocol == 'smb':
        if not check_tool('mount.cifs'):
            r = _apt_install('cifs-utils', timeout=60)
            if r.returncode != 0:
                return jsonify({'error': 'Failed to install cifs-utils'}), 500
    else:
        if not check_tool('mount.nfs'):
            r = _apt_install('nfs-common', timeout=60)
            if r.returncode != 0:
                return jsonify({'error': 'Failed to install nfs-common'}), 500

    # Generate ID
    safe_host = re.sub(r'[^a-zA-Z0-9._-]', '_', host_addr)
    safe_share = re.sub(r'[^a-zA-Z0-9._-]', '_', share)
    mount_id = f"{protocol}_{safe_host}_{safe_share}"

    if not name:
        name = f"{share} on {host_addr}"

    cfg = {
        'id': mount_id,
        'name': name,
        'protocol': protocol,
        'host': host_addr,
        'share': share,
        'username': username,
        'password': password,
        'domain': domain,
        'auto_mount': auto_mount,
    }

    # Check for duplicate
    mounts = _load_network_mounts()
    mounts = [m for m in mounts if m['id'] != mount_id]

    # Try to mount
    mp = _netmount_path(mount_id)
    if _is_mounted(mp):
        host_run(f"umount -l {Q(mp)}", timeout=10)

    if protocol == 'smb':
        ok, err = _mount_smb(cfg)
    else:
        ok, err = _mount_nfs(cfg)

    if not ok:
        host_run(f"rmdir {Q(mp)} 2>/dev/null", timeout=3)
        return jsonify({'error': f'Mount failed: {err}'}), 500

    # Save (without password in plain text — encode it)
    save_cfg = dict(cfg)
    if password:
        save_cfg['password'] = base64.b64encode(password.encode()).decode()
        save_cfg['_pw_enc'] = True
    mounts.append(save_cfg)
    _save_network_mounts(mounts)

    # Add to fstab if auto_mount
    if auto_mount:
        _netmount_fstab_add(cfg)

    _invalidate_drives_cache()
    return jsonify({
        'ok': True,
        'id': mount_id,
        'mount_path': mp,
        'name': name,
    })


@storage_bp.route('/network/unmount', methods=['POST'])
@admin_required
def network_mount_unmount():
    """Unmount a network drive (keep config)."""
    data = request.json or {}
    mount_id = data.get('id', '').strip()
    if not mount_id:
        return jsonify({'error': 'id is required'}), 400

    mp = _netmount_path(mount_id)
    if _is_mounted(mp):
        r = host_run(f"umount -l {Q(mp)}", timeout=15)
        if r.returncode != 0:
            return jsonify({'error': f'Unmount failed: {r.stderr.strip()}'}), 500

    _invalidate_drives_cache()
    return jsonify({'ok': True})


@storage_bp.route('/network/reconnect', methods=['POST'])
@admin_required
def network_mount_reconnect():
    """Reconnect (re-mount) a saved network drive."""
    data = request.json or {}
    mount_id = data.get('id', '').strip()
    if not mount_id:
        return jsonify({'error': 'id is required'}), 400

    mounts = _load_network_mounts()
    cfg = next((m for m in mounts if m['id'] == mount_id), None)
    if not cfg:
        return jsonify({'error': 'Network mount not found'}), 404

    # Decode password if encoded
    if cfg.get('_pw_enc') and cfg.get('password'):
        try:
            cfg['password'] = base64.b64decode(cfg['password']).decode()
        except Exception:
            pass

    mp = _netmount_path(mount_id)
    if _is_mounted(mp):
        host_run(f"umount -l {Q(mp)}", timeout=10)

    protocol = cfg.get('protocol', 'smb')
    if protocol == 'smb':
        ok, err = _mount_smb(cfg)
    else:
        ok, err = _mount_nfs(cfg)

    if not ok:
        return jsonify({'error': f'Mount failed: {err}'}), 500

    return jsonify({'ok': True, 'mount_path': mp})


@storage_bp.route('/network/remove', methods=['POST'])
@admin_required
def network_mount_remove():
    """Remove a network drive — unmount and delete config."""
    data = request.json or {}
    mount_id = data.get('id', '').strip()
    if not mount_id:
        return jsonify({'error': 'id is required'}), 400

    mp = _netmount_path(mount_id)
    if _is_mounted(mp):
        host_run(f"umount -l {Q(mp)}", timeout=15)
    host_run(f"rmdir {Q(mp)} 2>/dev/null", timeout=3)

    mounts = _load_network_mounts()
    mounts = [m for m in mounts if m['id'] != mount_id]
    _save_network_mounts(mounts)

    _netmount_fstab_remove(mount_id)

    _invalidate_drives_cache()
    return jsonify({'ok': True})


@storage_bp.route('/network/browse', methods=['POST'])
@admin_required
def network_browse():
    """Browse available SMB shares on a remote host."""
    data = request.json or {}
    host_addr = data.get('host', '').strip()
    username = data.get('username', '').strip()
    password = data.get('password', '')

    if not host_addr:
        return jsonify({'error': 'Host address is required'}), 400

    # Try smbclient -L to list shares
    if not check_tool('smbclient'):
        _apt_install('smbclient', timeout=60)

    auth_part = ''
    if username:
        auth_part = f"-U {Q(username + ('%' + password if password else '%'))}"
    else:
        auth_part = '-N'

    r = host_run(f"smbclient -L {Q(host_addr)} {auth_part} --no-pass 2>/dev/null || smbclient -L {Q(host_addr)} {auth_part} 2>&1", timeout=10)
    shares = []
    for line in r.stdout.splitlines():
        line = line.strip()
        # Parse smbclient output: "ShareName   Disk   Comment"
        if '\tDisk' in line or '  Disk  ' in line:
            parts = re.split(r'\s{2,}|\t', line.strip())
            if parts:
                share_name = parts[0].strip()
                comment = parts[2].strip() if len(parts) > 2 else ''
                if not share_name.endswith('$'):
                    shares.append({'name': share_name, 'comment': comment})

    return jsonify({'shares': shares})


@storage_bp.route('/network/scan')
@admin_required
def network_scan():
    """Scan local network for SMB/NFS servers."""
    servers = []

    # Use avahi-browse for mDNS discovery
    r = host_run("avahi-browse -t -r _smb._tcp 2>/dev/null | grep -E '^\\s+address|hostname' || true", timeout=8)
    seen = set()
    current_host = ''
    for line in r.stdout.splitlines():
        line = line.strip()
        if 'hostname' in line.lower():
            match = re.search(r'\[(.+?)\]', line)
            if match:
                current_host = match.group(1).rstrip('.')
        elif 'address' in line.lower():
            match = re.search(r'\[(.+?)\]', line)
            if match:
                addr = match.group(1)
                key = addr
                if key not in seen:
                    seen.add(key)
                    servers.append({
                        'host': addr,
                        'hostname': current_host or addr,
                        'protocols': ['smb'],
                    })

    # Fallback: nmap ping scan + SMB port check on local subnet
    if not servers:
        subnet = _detect_lan_subnet()
        if subnet:
            r = host_run(f"nmap -sn {Q(subnet)} --open -oG - 2>/dev/null | grep 'Status: Up' | awk '{{print $2}}' | head -20", timeout=15)
            for ip in r.stdout.strip().splitlines():
                ip = ip.strip()
                if ip and ip not in seen:
                    # Quick SMB port check
                    r2 = host_run(f"timeout 2 bash -c 'echo >/dev/tcp/{Q(ip)}/445' 2>/dev/null", timeout=5)
                    protocols = []
                    if r2.returncode == 0:
                        protocols.append('smb')
                    r3 = host_run(f"timeout 2 bash -c 'echo >/dev/tcp/{Q(ip)}/2049' 2>/dev/null", timeout=5)
                    if r3.returncode == 0:
                        protocols.append('nfs')
                    if protocols:
                        seen.add(ip)
                        servers.append({'host': ip, 'hostname': ip, 'protocols': protocols})

    return jsonify({'servers': servers})


def _netmount_fstab_add(cfg):
    """Add a network mount to fstab for auto-mount on boot."""
    mp = _netmount_path(cfg['id'])
    protocol = cfg.get('protocol', 'smb')

    if protocol == 'smb':
        creds_file = os.path.join(_NETMOUNT_DIR, f".{cfg['id']}.creds")
        username = cfg.get('username', 'guest')
        password = cfg.get('password', '')
        cred_content = f"username={username}\npassword={password}\n"
        raw = cred_content.encode('utf-8')
        b64 = base64.b64encode(raw).decode('ascii')
        host_run(f"mkdir -p {Q(_NETMOUNT_DIR)}")
        host_run(f"echo '{b64}' | base64 -d > {Q(creds_file)} && chmod 600 {Q(creds_file)}")

        entry = f"//{cfg['host']}/{cfg['share']}  {mp}  cifs  credentials={creds_file},iocharset=utf8,file_mode=0777,dir_mode=0777,noperm,noauto,x-systemd.automount,_netdev  0  0"
    else:
        entry = f"{cfg['host']}:{cfg['share']}  {mp}  nfs  rw,soft,timeo=30,retrans=3,noauto,x-systemd.automount,_netdev  0  0"

    # Remove old entry if exists, then append
    _netmount_fstab_remove(cfg['id'])
    tag = f"# ethos-netmount:{cfg['id']}"
    host_run(f"echo {Q(tag)} >> /etc/fstab && echo {Q(entry)} >> /etc/fstab")
    host_run("systemctl daemon-reload", timeout=10)


def _netmount_fstab_remove(mount_id):
    """Remove a network mount from fstab."""
    tag = f"ethos-netmount:{mount_id}"
    host_run(f"sed -i '/{tag}/d' /etc/fstab && sed -i '\\|{_NETMOUNT_DIR}/{mount_id}|d' /etc/fstab")
    host_run("systemctl daemon-reload", timeout=10)


def _auto_mount_network_drives():
    """Called at startup — mount all network drives flagged auto_mount."""
    mounts = _load_network_mounts()
    for cfg in mounts:
        if not cfg.get('auto_mount', False):
            continue
        mp = _netmount_path(cfg['id'])
        if _is_mounted(mp):
            continue
        # Decode password
        if cfg.get('_pw_enc') and cfg.get('password'):
            try:
                cfg['password'] = base64.b64decode(cfg['password']).decode()
            except Exception:
                pass
        protocol = cfg.get('protocol', 'smb')
        try:
            if protocol == 'smb':
                _mount_smb(cfg)
            else:
                _mount_nfs(cfg)
        except Exception as e:
            logging.getLogger(__name__).warning("Auto-mount %s failed: %s", cfg['id'], e)
