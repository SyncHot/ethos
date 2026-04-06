"""Cloud Backup blueprint – rclone-based cloud backup for EthOS NAS."""

import json
import os
import re
import shlex
import subprocess
import threading
import time
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from blueprints.admin_required import admin_required
from host import host_run, data_path

from utils import load_json, save_json

cloud_backup_bp = Blueprint('cloud_backup', __name__, url_prefix='/api/cloud-backup')

CONFIG_FILE = data_path('cloud_backup.json')
HISTORY_FILE = data_path('cloud_backup_history.json')

_running_jobs = {}
_lock = threading.Lock()

# ── rclone helpers ──────────────────────────────────────────

def _ensure_rclone():
    """Install rclone if not present. Returns (ok, error_msg)."""
    if host_run('which rclone', timeout=5).returncode == 0:
        return True, None
    r = host_run('curl -s https://rclone.org/install.sh | bash && apt-get clean 2>/dev/null', timeout=120)
    if r.returncode != 0:
        return False, f'Failed to install rclone: {r.stderr.strip()}'
    return True, None


def _rclone_cmd(args_str, timeout=30):
    """Run an rclone command and return CompletedProcess."""
    return host_run(f'rclone {args_str}', timeout=timeout)


def _load_config():
    return load_json(CONFIG_FILE, {'jobs': []})


def _save_config(cfg):
    save_json(CONFIG_FILE, cfg)


def _load_history():
    return load_json(HISTORY_FILE, [])


def _save_history(history):
    save_json(HISTORY_FILE, history)


def _add_history(entry):
    history = _load_history()
    history.insert(0, entry)
    history = history[:200]
    _save_history(history)


def _find_job(cfg, job_id):
    for j in cfg['jobs']:
        if j['id'] == job_id:
            return j
    return None


# ── Cron helpers ────────────────────────────────────────────

CRON_TAG = '# ethos-cloud-backup'


def _build_cron_line(job):
    """Build a cron line for a job, or None if not scheduled."""
    sched = job.get('schedule', {})
    if not sched.get('enabled'):
        return None
    cron_expr = sched.get('cron', '0 2 * * *')
    job_id = shlex.quote(job['id'])
    line = f'{cron_expr} root /usr/bin/curl -s -X POST -H "Content-Length: 0" http://127.0.0.1:5000/api/cloud-backup/jobs/{job_id}/run {CRON_TAG} id={job["id"]}'
    return line


def _sync_cron():
    """Rewrite the cloud-backup cron file based on current config."""
    cfg = _load_config()
    lines = []
    for job in cfg['jobs']:
        line = _build_cron_line(job)
        if line:
            lines.append(line)

    cron_path = '/etc/cron.d/ethos-cloud-backup'
    if lines:
        content = 'SHELL=/bin/bash\nPATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
        content += '\n'.join(lines) + '\n'
        try:
            with open(cron_path, 'w') as f:
                f.write(content)
            os.chmod(cron_path, 0o644)
        except PermissionError:
            host_run(f'echo {shlex.quote(content)} | sudo tee {shlex.quote(cron_path)} > /dev/null && sudo chmod 644 {shlex.quote(cron_path)}', timeout=10)
    else:
        if os.path.exists(cron_path):
            try:
                os.remove(cron_path)
            except PermissionError:
                host_run(f'sudo rm -f {shlex.quote(cron_path)}', timeout=5)


# ── Provider type → rclone config params mapping ───────────

PROVIDER_TYPES = {
    's3': {
        'rclone_type': 's3',
        'fields': ['provider', 'access_key_id', 'secret_access_key', 'region', 'endpoint'],
    },
    'b2': {
        'rclone_type': 'b2',
        'fields': ['account', 'key'],
    },
    'gdrive': {
        'rclone_type': 'drive',
        'fields': ['client_id', 'client_secret', 'token'],
    },
    'webdav': {
        'rclone_type': 'webdav',
        'fields': ['url', 'vendor', 'user', 'pass'],
    },
    'sftp': {
        'rclone_type': 'sftp',
        'fields': ['host', 'user', 'port', 'key_file', 'pass'],
    },
}

# ── Routes: Providers ──────────────────────────────────────

@cloud_backup_bp.route('/providers', methods=['GET'])
@admin_required
def list_providers():
    ok, err = _ensure_rclone()
    if not ok:
        return jsonify({'error': err, 'remotes': [], 'not_installed': True}), 503

    r = _rclone_cmd('listremotes', timeout=10)
    if r.returncode != 0:
        return jsonify({'remotes': []})

    remotes = []
    for line in r.stdout.strip().splitlines():
        name = line.strip().rstrip(':')
        if name:
            info_r = _rclone_cmd(f'config show {shlex.quote(name)}', timeout=10)
            rtype = ''
            if info_r.returncode == 0:
                for il in info_r.stdout.splitlines():
                    if il.strip().startswith('type'):
                        rtype = il.split('=', 1)[-1].strip()
                        break
            remotes.append({'name': name, 'type': rtype})
    return jsonify({'remotes': remotes})


@cloud_backup_bp.route('/providers', methods=['POST'])
@admin_required
def add_provider():
    ok, err = _ensure_rclone()
    if not ok:
        return jsonify({'error': err}), 500

    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    ptype = data.get('type', '').strip()

    if not name or not re.match(r'^[a-zA-Z0-9_-]+$', name):
        return jsonify({'error': 'Invalid remote name (use alphanumeric, dash, underscore)'}), 400
    if ptype not in PROVIDER_TYPES:
        return jsonify({'error': f'Unsupported type: {ptype}'}), 400

    spec = PROVIDER_TYPES[ptype]
    params = data.get('params', {})

    # Build rclone config create command
    cmd_parts = ['config', 'create', shlex.quote(name), shlex.quote(spec['rclone_type'])]
    for field in spec['fields']:
        val = params.get(field, '')
        if val:
            cmd_parts.append(f'{shlex.quote(field)}={shlex.quote(str(val))}')

    r = _rclone_cmd(' '.join(cmd_parts), timeout=30)
    if r.returncode != 0:
        return jsonify({'error': f'rclone config failed: {r.stderr.strip()}'}), 500

    return jsonify({'success': True, 'name': name})


@cloud_backup_bp.route('/providers/<name>', methods=['DELETE'])
@admin_required
def delete_provider(name):
    ok, err = _ensure_rclone()
    if not ok:
        return jsonify({'error': err}), 500

    r = _rclone_cmd(f'config delete {shlex.quote(name)}', timeout=10)
    if r.returncode != 0:
        return jsonify({'error': f'Failed to delete: {r.stderr.strip()}'}), 500
    return jsonify({'success': True})


# ── Routes: Jobs ───────────────────────────────────────────

@cloud_backup_bp.route('/jobs', methods=['GET'])
@admin_required
def list_jobs():
    cfg = _load_config()
    jobs = cfg.get('jobs', [])
    with _lock:
        for j in jobs:
            run_info = _running_jobs.get(j['id'])
            if run_info:
                j['running'] = True
                j['progress'] = run_info.get('progress', {})
            else:
                j['running'] = False
    return jsonify({'jobs': jobs})


@cloud_backup_bp.route('/jobs', methods=['POST'])
@admin_required
def create_job():
    data = request.get_json(force=True)
    cfg = _load_config()

    job = {
        'id': str(uuid.uuid4())[:8],
        'name': data.get('name', 'Untitled'),
        'source': data.get('source', ''),
        'remote': data.get('remote', ''),
        'remote_path': data.get('remote_path', '/'),
        'schedule': data.get('schedule', {'enabled': False, 'cron': '0 2 * * *'}),
        'retention': data.get('retention', {'enabled': False, 'days': 30}),
        'created': datetime.now().isoformat(),
        'last_run': None,
        'last_status': None,
    }

    if not job['source'] or not job['remote']:
        return jsonify({'error': 'source and remote are required'}), 400

    cfg['jobs'].append(job)
    _save_config(cfg)
    _sync_cron()
    return jsonify({'success': True, 'job': job})


@cloud_backup_bp.route('/jobs/<job_id>', methods=['PUT'])
@admin_required
def update_job(job_id):
    data = request.get_json(force=True)
    cfg = _load_config()
    job = _find_job(cfg, job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    for key in ('name', 'source', 'remote', 'remote_path', 'schedule', 'retention'):
        if key in data:
            job[key] = data[key]

    _save_config(cfg)
    _sync_cron()
    return jsonify({'success': True, 'job': job})


@cloud_backup_bp.route('/jobs/<job_id>', methods=['DELETE'])
@admin_required
def delete_job(job_id):
    cfg = _load_config()
    cfg['jobs'] = [j for j in cfg['jobs'] if j['id'] != job_id]
    _save_config(cfg)
    _sync_cron()
    return jsonify({'success': True})


# ── Run / Status / Restore ─────────────────────────────────

def _run_backup_thread(job_id):
    """Background thread that runs rclone sync with progress tracking."""
    cfg = _load_config()
    job = _find_job(cfg, job_id)
    if not job:
        return

    source = job['source']
    remote = job['remote']
    remote_path = job.get('remote_path', '/')
    dest = f'{remote}:{remote_path}'

    start_time = time.time()
    entry = {
        'id': str(uuid.uuid4())[:8],
        'job_id': job_id,
        'job_name': job.get('name', ''),
        'started': datetime.now().isoformat(),
        'finished': None,
        'status': 'running',
        'bytes_transferred': 0,
        'files_transferred': 0,
        'errors': 0,
        'duration': 0,
        'message': '',
    }

    cmd = f'rclone sync {shlex.quote(source)} {shlex.quote(dest)} --stats 1s --stats-log-level NOTICE --log-format "" -v 2>&1'

    try:
        proc = subprocess.Popen(
            ['bash', '-c', cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        last_progress = {}
        for line in iter(proc.stdout.readline, ''):
            line = line.strip()
            # Parse transfer stats from rclone output
            if 'Transferred:' in line and 'ETA' in line:
                last_progress['detail'] = line
            elif 'Transferred:' in line:
                parts = line.split('Transferred:')
                if len(parts) > 1:
                    last_progress['transferred'] = parts[1].strip()
            elif 'Errors:' in line:
                try:
                    last_progress['errors'] = int(re.search(r'Errors:\s*(\d+)', line).group(1))
                except Exception:
                    pass
            elif 'Checks:' in line:
                try:
                    last_progress['checks'] = int(re.search(r'Checks:\s*(\d+)', line).group(1))
                except Exception:
                    pass
            elif re.match(r'.*\d+%.*', line):
                pct_match = re.search(r'(\d+)%', line)
                if pct_match:
                    last_progress['percent'] = int(pct_match.group(1))

            with _lock:
                if job_id in _running_jobs:
                    _running_jobs[job_id]['progress'] = dict(last_progress)

        proc.wait()
        elapsed = time.time() - start_time

        entry['finished'] = datetime.now().isoformat()
        entry['duration'] = round(elapsed, 1)
        entry['errors'] = last_progress.get('errors', 0)
        entry['status'] = 'success' if proc.returncode == 0 else 'error'
        if proc.returncode != 0:
            entry['message'] = f'rclone exited with code {proc.returncode}'

    except Exception as exc:
        entry['finished'] = datetime.now().isoformat()
        entry['duration'] = round(time.time() - start_time, 1)
        entry['status'] = 'error'
        entry['message'] = str(exc)

    finally:
        with _lock:
            _running_jobs.pop(job_id, None)

        cfg = _load_config()
        job = _find_job(cfg, job_id)
        if job:
            job['last_run'] = entry['finished']
            job['last_status'] = entry['status']
            _save_config(cfg)

        _add_history(entry)


@cloud_backup_bp.route('/jobs/<job_id>/run', methods=['POST'])
@admin_required
def run_job(job_id):
    ok, err = _ensure_rclone()
    if not ok:
        return jsonify({'error': err}), 500

    cfg = _load_config()
    job = _find_job(cfg, job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    with _lock:
        if job_id in _running_jobs:
            return jsonify({'error': 'Job is already running'}), 409
        _running_jobs[job_id] = {'progress': {}, 'started': time.time()}

    t = threading.Thread(target=_run_backup_thread, args=(job_id,), daemon=True)
    t.start()
    return jsonify({'status': 'ok'})


@cloud_backup_bp.route('/jobs/<job_id>/status', methods=['GET'])
@admin_required
def job_status(job_id):
    with _lock:
        run_info = _running_jobs.get(job_id)
    if run_info:
        return jsonify({'running': True, 'progress': run_info.get('progress', {})})

    history = _load_history()
    for entry in history:
        if entry.get('job_id') == job_id:
            return jsonify({'running': False, 'last_run': entry})
    return jsonify({'running': False, 'last_run': None})


@cloud_backup_bp.route('/jobs/<job_id>/restore', methods=['POST'])
@admin_required
def restore_job(job_id):
    ok, err = _ensure_rclone()
    if not ok:
        return jsonify({'error': err}), 500

    cfg = _load_config()
    job = _find_job(cfg, job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404

    data = request.get_json(force=True) or {}
    dest = data.get('destination', job['source'])
    remote = job['remote']
    remote_path = job.get('remote_path', '/')
    src = f'{remote}:{remote_path}'

    r = host_run(
        f'rclone sync {shlex.quote(src)} {shlex.quote(dest)} -v',
        timeout=3600,
    )
    if r.returncode != 0:
        return jsonify({'error': f'Restore failed: {r.stderr.strip()}'}), 500
    return jsonify({'status': 'ok'})


# ── History ────────────────────────────────────────────────

@cloud_backup_bp.route('/history', methods=['GET'])
@admin_required
def get_history():
    history = _load_history()
    limit = request.args.get('limit', 50, type=int)
    return jsonify({'history': history[:limit]})


@cloud_backup_bp.route('/pkg-status')
@admin_required
def pkg_status():
    """Package status for AppStore integration."""
    import shutil
    installed = shutil.which('rclone') is not None
    return jsonify({'installed': installed, 'status': 'active' if installed else 'not_installed'})


@cloud_backup_bp.route('/install', methods=['POST'])
@admin_required
def install_rclone():
    """Install rclone for cloud backup."""
    from host import host_run
    host_run('curl -fsSL https://rclone.org/install.sh | bash', timeout=120)
    import shutil
    ok = shutil.which('rclone') is not None
    return jsonify({'status': 'ok' if ok else 'error', 'installed': ok})


@cloud_backup_bp.route('/uninstall', methods=['POST'])
@admin_required
def uninstall_rclone():
    """Uninstall rclone."""
    from host import host_run
    host_run('apt-get remove -y rclone 2>/dev/null; rm -f /usr/bin/rclone', timeout=60)
    return jsonify({'ok': True})
