"""
EthOS — Antivirus (ClamAV)

Endpoints:
  GET  /api/antivirus/pkg-status         — check if clamav installed
  POST /api/antivirus/install            — install clamav (SocketIO: antivirus_install)
  POST /api/antivirus/uninstall          — remove clamav
  POST /api/antivirus/migrate-db         — migrate virus DB from /var/lib to /mnt/data
  GET  /api/antivirus/status             — engine version, DB date, active scan info
  POST /api/antivirus/scan               — start on-demand scan (SocketIO: antivirus_scan)
  POST /api/antivirus/scan/cancel        — cancel running scan
  GET  /api/antivirus/results            — scan history (last 50)
  DELETE /api/antivirus/results/<id>     — delete a result entry
  GET  /api/antivirus/schedules          — list scheduled scans
  POST /api/antivirus/schedules          — create scheduled scan
  PUT  /api/antivirus/schedules/<sid>    — update scheduled scan
  DELETE /api/antivirus/schedules/<sid>  — delete scheduled scan
  POST /api/antivirus/update-db          — update virus definitions (SocketIO: antivirus_update_db)
"""

import os
import json
import re
import shutil
import subprocess
import threading
import secrets
import time
from datetime import datetime
from flask import Blueprint, jsonify, request
from blueprints.admin_required import admin_required
from host import data_path, q, host_run, apt_install, get_data_disk as _get_data_disk

antivirus_bp = Blueprint('antivirus', __name__, url_prefix='/api/antivirus')

# ─── Paths ────────────────────────────────────────────────────────────────────
SCHEDULES_FILE = data_path('antivirus_schedules.json')
RESULTS_FILE   = data_path('antivirus_results.json')
SCAN_LOGS_DIR  = data_path('av_scan_logs')
MAX_RESULTS    = 50
_CRON_MARKER   = '# ETHOS_AV:'
_CLAMAV_DEFAULT_DB = '/var/lib/clamav'
_clamav_migrated = False  # guard — run auto-migration once per process


def _clamav_db_dir():
    """Return ClamAV database directory — data partition preferred over root."""
    dd = _get_data_disk()
    if dd:
        p = os.path.join(dd, 'clamav')
        os.makedirs(p, exist_ok=True)
        return p
    return _CLAMAV_DEFAULT_DB


def _do_clamav_migration():
    """Move existing ClamAV DB from /var/lib/clamav to data partition if needed.
    Safe to call multiple times — no-ops if already migrated or no data disk."""
    global _clamav_migrated
    if _clamav_migrated:
        return {'moved': 0, 'skipped': True}
    _clamav_migrated = True

    target = _clamav_db_dir()
    if target == _CLAMAV_DEFAULT_DB:
        return {'moved': 0, 'skipped': True, 'reason': 'no data disk'}

    src = _CLAMAV_DEFAULT_DB
    if not os.path.isdir(src):
        return {'moved': 0, 'skipped': True, 'reason': 'source missing'}

    # Check if config already points to target (already migrated)
    conf_file = '/etc/clamav/freshclam.conf'
    if os.path.isfile(conf_file):
        with open(conf_file) as f:
            if target in f.read():
                return {'moved': 0, 'skipped': True, 'reason': 'already configured'}

    # Move files
    moved = 0
    errors = []
    try:
        host_run('systemctl stop clamav-freshclam 2>/dev/null', timeout=15)
        host_run('systemctl stop clamav-daemon 2>/dev/null', timeout=15)
    except Exception:
        pass
    try:
        for fname in os.listdir(src):
            src_f = os.path.join(src, fname)
            dst_f = os.path.join(target, fname)
            if os.path.isfile(src_f) and not os.path.exists(dst_f):
                shutil.move(src_f, dst_f)
                moved += 1
    except Exception as e:
        errors.append(str(e))

    # Patch config files
    for conf in ('/etc/clamav/freshclam.conf', '/etc/clamav/clamd.conf'):
        if os.path.isfile(conf):
            try:
                txt = open(conf).read()
                txt = re.sub(r'^DatabaseDirectory\s.*$',
                             f'DatabaseDirectory {target}', txt, flags=re.MULTILINE)
                if 'DatabaseDirectory' not in txt:
                    txt += f'\nDatabaseDirectory {target}\n'
                open(conf, 'w').write(txt)
            except Exception as e:
                errors.append(f'{conf}: {e}')

    host_run(f'chown -R clamav:clamav {q(target)} 2>/dev/null', timeout=10)
    try:
        host_run('systemctl start clamav-freshclam 2>/dev/null', timeout=15)
    except Exception:
        pass

    return {'moved': moved, 'errors': errors, 'target': target}


# ─── Auto-migration on import (best-effort) ──────────────────────────────────
try:
    _do_clamav_migration()
except Exception:
    pass


# ─── Active scan state ────────────────────────────────────────────────────────
_active_scan = {}


# ─── JSON helpers ─────────────────────────────────────────────────────────────

def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default


def _save_json(path, data):
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _load_schedules():
    return _load_json(SCHEDULES_FILE, [])

def _save_schedules(s):
    _save_json(SCHEDULES_FILE, s)

def _load_results():
    return _load_json(RESULTS_FILE, [])

def _save_results(r):
    _save_json(RESULTS_FILE, r)

def _append_result(result):
    results = _load_results()
    results.insert(0, result)
    _save_results(results[:MAX_RESULTS])


# ─── ClamAV helpers ───────────────────────────────────────────────────────────

def _is_installed():
    return bool(shutil.which('clamscan'))


def _clamav_version():
    try:
        r = host_run('clamscan --version', timeout=10)
        if r.returncode == 0:
            return r.stdout.strip().split('\n')[0]
    except Exception:
        pass
    return None


def _db_info():
    db_dir = _clamav_db_dir()
    for fname in ('daily.info', 'main.info'):
        try:
            fpath = os.path.join(db_dir, fname)
            if os.path.exists(fpath):
                with open(fpath) as f:
                    for line in f:
                        if line.startswith('BuildDate:'):
                            return line.split(':', 1)[1].strip()
        except Exception:
            pass
    try:
        r = host_run('clamscan --version', timeout=10)
        if r.returncode == 0:
            m = re.search(r'/(\d+)/', r.stdout)
            if m:
                return 'DB ' + m.group(1)
    except Exception:
        pass
    return None


# ─── Crontab helpers ──────────────────────────────────────────────────────────

def _ensure_cron():
    """Install cron daemon if not present."""
    if shutil.which('crontab'):
        return True
    try:
        r = apt_install('cron', timeout=120)
        return r.returncode == 0
    except Exception:
        return False


def _read_crontab():
    if not _ensure_cron():
        return []
    try:
        r = host_run('crontab -l', timeout=10)
        if r.returncode != 0:
            return []
        return r.stdout.splitlines()
    except Exception:
        return []


def _write_crontab(lines):
    if not _ensure_cron():
        return False, 'cron not available'
    content = '\n'.join(lines)
    if content and not content.endswith('\n'):
        content += '\n'
    try:
        r = subprocess.run(
            ['crontab', '-'],
            input=content, capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0, r.stderr.strip()
    except Exception as e:
        return False, str(e)


def _sync_schedule_to_cron(schedule):
    os.makedirs(SCAN_LOGS_DIR, exist_ok=True)
    sid       = schedule['id']
    log_file  = os.path.join(SCAN_LOGS_DIR, sid + '.log')
    scan_path = schedule.get('path', '/home')
    enabled   = schedule.get('enabled', True)
    cron_expr = schedule.get('cron_expr', '0 2 * * 0')
    marker    = _CRON_MARKER + sid

    lines     = _read_crontab()
    new_lines = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if line.strip() == marker:
            skip_next = True
            continue
        new_lines.append(line)

    if enabled:
        new_lines.append(marker)
        new_lines.append(
            cron_expr + '  clamscan -r --infected'
            ' --log=' + q(log_file) + ' ' + q(scan_path) + ' > /dev/null 2>&1'
        )
    _write_crontab(new_lines)


def _remove_schedule_from_cron(sid):
    marker    = _CRON_MARKER + sid
    lines     = _read_crontab()
    new_lines = []
    skip_next = False
    for line in lines:
        if skip_next:
            skip_next = False
            continue
        if line.strip() == marker:
            skip_next = True
            continue
        new_lines.append(line)
    _write_crontab(new_lines)


def _parse_scan_log(log_path):
    if not os.path.exists(log_path):
        return None
    try:
        with open(log_path) as f:
            content = f.read()
        result = {
            'infected_files': [],
            'scanned_files': 0,
            'infected_count': 0,
            'scan_time': None,
            'data_scanned': None,
            'finished_at': datetime.fromtimestamp(os.path.getmtime(log_path)).isoformat(),
        }
        for line in content.splitlines():
            if 'FOUND' in line:
                result['infected_files'].append(line.strip())
            m = re.search(r'Scanned files:\s+(\d+)', line)
            if m:
                result['scanned_files'] = int(m.group(1))
            m = re.search(r'Infected files:\s+(\d+)', line)
            if m:
                result['infected_count'] = int(m.group(1))
            m = re.search(r'Time:\s+([\d.]+)\s+sec', line)
            if m:
                result['scan_time'] = float(m.group(1))
            m = re.search(r'Data scanned:\s+([\d.]+\s+\w+)', line)
            if m:
                result['data_scanned'] = m.group(1)
        return result
    except Exception:
        return None


def _validate_cron_expr(expr):
    """Strict cron expression validation — prevents shell injection."""
    import re
    parts = expr.strip().split()
    if len(parts) != 5:
        return 'Cron expression must have 5 fields'
    _FIELD_RE = re.compile(
        r'^(\*|\d{1,2}(?:-\d{1,2})?(?:/\d{1,2})?'
        r'(?:,\d{1,2}(?:-\d{1,2})?(?:/\d{1,2})?)*)$'
    )
    limits = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    names  = ['minute', 'hour', 'day', 'month', 'weekday']
    for part, (lo, hi), name in zip(parts, limits, names):
        if not _FIELD_RE.match(part):
            return f'Invalid {name}: {part}'
        for tok in re.split(r'[,/\-]', part):
            if tok == '*':
                continue
            if tok.isdigit() and not (lo <= int(tok) <= hi):
                return f'{name} value {tok} out of range ({lo}-{hi})'
    return None


# ─── Routes ───────────────────────────────────────────────────────────────────

@antivirus_bp.route('/pkg-status', methods=['GET'])
@admin_required
def pkg_status():
    return jsonify({'installed': _is_installed()})


@antivirus_bp.route('/status', methods=['GET'])
@admin_required
def get_status():
    if not _is_installed():
        return jsonify({'installed': False})
    results = _load_results()
    return jsonify({
        'installed': True,
        'version': _clamav_version(),
        'db_info': _db_info(),
        'last_scan': results[0] if results else None,
        'scanning': bool(_active_scan),
        'active_scan': {
            'path': _active_scan.get('path'),
            'start_time': _active_scan.get('start_time'),
            'scan_id': _active_scan.get('scan_id'),
        } if _active_scan else None,
    })


@antivirus_bp.route('/scan', methods=['POST'])
@admin_required
def start_scan():
    if _active_scan:
        return jsonify({'error': 'Scan already running'}), 409

    data      = request.json or {}
    scan_path = os.path.realpath(data.get('path', '/home').strip())

    if not os.path.exists(scan_path):
        return jsonify({'error': 'Path does not exist: ' + scan_path}), 400

    scan_id   = secrets.token_hex(8)
    _socketio = getattr(antivirus_bp, '_socketio', None)

    def _bg():
        _active_scan.update({
            'proc': None,
            'path': scan_path,
            'start_time': datetime.now().isoformat(),
            'scan_id': scan_id,
        })

        def _emit(stage, pct, msg, **extra):
            if _socketio:
                _socketio.emit('antivirus_scan', dict(
                    scan_id=scan_id, stage=stage, percent=pct, message=msg, **extra
                ))

        _emit('start', 0, 'Scanning: ' + scan_path, path=scan_path)

        scanned = 0
        threats = []
        start   = time.time()
        total_estimate = 0

        # Quick file count for progress percentage
        try:
            count_proc = subprocess.run(
                ['find', scan_path, '-type', 'f'],
                capture_output=True, text=True, timeout=15,
            )
            total_estimate = max(count_proc.stdout.count('\n'), 1)
        except Exception:
            total_estimate = 0

        try:
            proc = subprocess.Popen(
                ['clamscan', '-r', '--infected', scan_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            _active_scan['proc'] = proc

            for line in iter(proc.stdout.readline, ''):
                line = line.rstrip()
                if not line:
                    continue
                if line.endswith(': OK'):
                    scanned += 1
                    if scanned % 50 == 0:
                        pct = min(int(scanned * 95 / total_estimate), 95) if total_estimate else -1
                        _emit('progress', pct,
                              'Scanned ' + str(scanned) + ' files...',
                              scanned=scanned, threats=len(threats))
                elif ': ' in line and 'FOUND' in line:
                    scanned += 1
                    threats.append(line)
                    pct = min(int(scanned * 95 / total_estimate), 95) if total_estimate else -1
                    _emit('threat', pct, 'Threat: ' + line,
                          threat=line, scanned=scanned, threats=len(threats))
                else:
                    m = re.search(r'Scanned files:\s+(\d+)', line)
                    if m:
                        scanned = max(scanned, int(m.group(1)))

            proc.wait()
            elapsed = round(time.time() - start, 1)

            if not _active_scan:
                return

            if proc.returncode == 2:
                _emit('error', 0, 'ClamAV scanner error')
                _active_scan.clear()
                return

            result = {
                'id': scan_id,
                'type': 'manual',
                'path': scan_path,
                'started_at': _active_scan.get('start_time'),
                'finished_at': datetime.now().isoformat(),
                'scanned_files': scanned,
                'infected_count': len(threats),
                'infected_files': threats[:100],
                'scan_time': elapsed,
                'status': 'clean' if not threats else 'threats_found',
            }
            _append_result(result)
            _emit('done', 100,
                  'Scan done. Files: ' + str(scanned) + ', Threats: ' + str(len(threats)),
                  result=result)

        except Exception as e:
            _emit('error', 0, str(e))
        finally:
            _active_scan.clear()

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True, 'scan_id': scan_id})


@antivirus_bp.route('/scan/cancel', methods=['POST'])
@admin_required
def cancel_scan():
    if not _active_scan:
        return jsonify({'error': 'No active scan'}), 404
    proc = _active_scan.get('proc')
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    _active_scan.clear()
    return jsonify({'ok': True})


@antivirus_bp.route('/results', methods=['GET'])
@admin_required
def get_results():
    return jsonify({'items': _load_results()})


@antivirus_bp.route('/results/<scan_id>', methods=['DELETE'])
@admin_required
def delete_result(scan_id):
    results = [r for r in _load_results() if r.get('id') != scan_id]
    _save_results(results)
    return jsonify({'ok': True})


@antivirus_bp.route('/schedules', methods=['GET'])
@admin_required
def get_schedules():
    schedules = _load_schedules()
    for s in schedules:
        log_path     = os.path.join(SCAN_LOGS_DIR, s['id'] + '.log')
        s['last_log'] = _parse_scan_log(log_path)
    return jsonify({'items': schedules})


@antivirus_bp.route('/schedules', methods=['POST'])
@admin_required
def create_schedule():
    data      = request.json or {}
    name      = data.get('name', '').strip()
    scan_path = os.path.realpath(data.get('path', '/home').strip())
    cron_expr = data.get('cron_expr', '0 2 * * 0').strip()
    enabled   = bool(data.get('enabled', True))

    if not name:
        return jsonify({'error': 'Name is required'}), 400
    err = _validate_cron_expr(cron_expr)
    if err:
        return jsonify({'error': err}), 400
    if not os.path.exists(scan_path):
        return jsonify({'error': 'Path does not exist: ' + scan_path}), 400

    schedule = {
        'id': secrets.token_hex(6),
        'name': name,
        'path': scan_path,
        'cron_expr': cron_expr,
        'enabled': enabled,
        'created_at': datetime.now().isoformat(),
    }
    schedules = _load_schedules()
    schedules.append(schedule)
    _save_schedules(schedules)
    _sync_schedule_to_cron(schedule)
    return jsonify({'ok': True, 'item': schedule})


@antivirus_bp.route('/schedules/<sid>', methods=['PUT'])
@admin_required
def update_schedule(sid):
    data      = request.json or {}
    schedules = _load_schedules()
    for s in schedules:
        if s['id'] == sid:
            if 'name' in data:
                s['name'] = str(data['name']).strip()
            if 'path' in data:
                s['path'] = str(data['path']).strip()
            if 'cron_expr' in data:
                expr = str(data['cron_expr']).strip()
                err  = _validate_cron_expr(expr)
                if err:
                    return jsonify({'error': err}), 400
                s['cron_expr'] = expr
            if 'enabled' in data:
                s['enabled'] = bool(data['enabled'])
            _save_schedules(schedules)
            _sync_schedule_to_cron(s)
            return jsonify({'ok': True, 'item': s})
    return jsonify({'error': 'Schedule not found'}), 404


@antivirus_bp.route('/schedules/<sid>', methods=['DELETE'])
@admin_required
def delete_schedule(sid):
    schedules = _load_schedules()
    new_sched = [s for s in schedules if s['id'] != sid]
    if len(new_sched) == len(schedules):
        return jsonify({'error': 'Schedule not found'}), 404
    _save_schedules(new_sched)
    _remove_schedule_from_cron(sid)
    try:
        os.remove(os.path.join(SCAN_LOGS_DIR, sid + '.log'))
    except Exception:
        pass
    return jsonify({'ok': True})


@antivirus_bp.route('/update-db', methods=['POST'])
@admin_required
def update_db():
    task_id   = secrets.token_hex(8)
    _socketio = getattr(antivirus_bp, '_socketio', None)

    def _bg():
        def _emit(stage, pct, msg):
            if _socketio:
                _socketio.emit('antivirus_update_db', {
                    'task_id': task_id, 'stage': stage, 'percent': pct, 'message': msg,
                })

        _emit('start', 10, 'Stopping freshclam service...')
        try:
            host_run('systemctl stop clamav-freshclam', timeout=15)
            _emit('start', 30, 'Downloading virus database updates...')
            r = host_run('freshclam --stdout --no-warnings', timeout=300)
            host_run('systemctl start clamav-freshclam', timeout=15)
            if r.returncode not in (0, 1):
                _emit('error', 0, ((r.stderr or r.stdout or 'Update error')[:300]))
                return
            _emit('done', 100, 'Virus database updated!')
        except Exception as e:
            host_run('systemctl start clamav-freshclam', timeout=15)
            _emit('error', 0, str(e))

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


@antivirus_bp.route('/install', methods=['POST'])
@admin_required
def install():
    task_id   = secrets.token_hex(8)
    _socketio = getattr(antivirus_bp, '_socketio', None)

    def _bg():
        def _emit(stage, pct, msg):
            if _socketio:
                _socketio.emit('antivirus_install', {
                    'task_id': task_id, 'stage': stage, 'percent': pct, 'message': msg,
                })

        _emit('start', 5, 'Installing ClamAV...')
        try:
            r = apt_install('clamav clamav-freshclam', timeout=300)
            if r.returncode != 0:
                _emit('error', 0, 'Install error: ' + (r.stderr or '')[:300])
                return

            # Redirect virus database to data partition if available
            db_dir = _clamav_db_dir()
            if db_dir != _CLAMAV_DEFAULT_DB:
                _emit('progress', 60, f'Konfigurowanie bazy wirusów → {db_dir}')
                try:
                    # Update freshclam.conf and clamd.conf to use data partition
                    for conf_file in ('/etc/clamav/freshclam.conf', '/etc/clamav/clamd.conf'):
                        if os.path.isfile(conf_file):
                            with open(conf_file) as _f:
                                content = _f.read()
                            # Replace or add DatabaseDirectory line
                            import re as _re
                            if _re.search(r'^DatabaseDirectory\s', content, _re.MULTILINE):
                                content = _re.sub(r'^DatabaseDirectory\s.*$',
                                                  f'DatabaseDirectory {db_dir}',
                                                  content, flags=_re.MULTILINE)
                            else:
                                content += f'\nDatabaseDirectory {db_dir}\n'
                            with open(conf_file, 'w') as _f:
                                _f.write(content)
                    # Ensure directory owned by clamav user
                    host_run(f'chown -R clamav:clamav {q(db_dir)}', timeout=10)
                except Exception as _e:
                    _emit('progress', 65, f'Uwaga: konfiguracja db_dir nie powiodła się: {_e}')

            _emit('progress', 70, 'Downloading virus database...')
            host_run('systemctl stop clamav-freshclam', timeout=15)
            host_run('freshclam --stdout --no-warnings', timeout=300)
            host_run('systemctl enable --now clamav-freshclam', timeout=15)
            _emit('done', 100, 'ClamAV installed!')
        except Exception as e:
            _emit('error', 0, str(e))

    threading.Thread(target=_bg, daemon=True).start()
    return jsonify({'ok': True, 'task_id': task_id})


@antivirus_bp.route('/migrate-db', methods=['POST'])
@admin_required
def migrate_db():
    """Migrate ClamAV database from /var/lib/clamav to data partition."""
    global _clamav_migrated
    _clamav_migrated = False  # force re-run
    result = _do_clamav_migration()
    return jsonify({'ok': True, **result})


@antivirus_bp.route('/uninstall', methods=['POST'])
@admin_required
def uninstall():
    for s in _load_schedules():
        _remove_schedule_from_cron(s['id'])
    try:
        host_run('apt-get remove -y clamav clamav-freshclam', timeout=120)
    except Exception:
        pass
    return jsonify({'ok': True})
