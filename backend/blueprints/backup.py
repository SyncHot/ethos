"""
EthOS — Backup & Restore Blueprint
Full and incremental backups with USB/SSH destinations, profiles, scheduling.
Migrated from standalone backuprestore app (eventlet → gevent).
"""

import os
import subprocess
import threading
import time
import json
import logging
import fcntl
import gevent
from datetime import datetime, timedelta
import shutil
from pathlib import Path
import re
import secrets
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError

from flask import Blueprint, jsonify, request, g

try:
    import paramiko
    from scp import SCPClient
    HAS_SSH = True
except ImportError:
    HAS_SSH = False

from ssh_utils import (
    get_ssh_client as _get_ssh_client,
    ssh_resolve_home as _ssh_resolve_home,
    ssh_ensure_writable_dir as _ssh_ensure_writable_dir,
    ssh_remote_disk_info as _ssh_remote_disk_info,
    ssh_exec as _ssh_exec,
)

from blueprints.profiles_db import get_db_connection, init_profiles_db

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from host import app_path, data_path, log_path
from crypto_utils import encrypt_secret, decrypt_secret

backup_bp = Blueprint('backup', __name__, url_prefix='/api/backup')

# Thread pool for filesystem calls that may hang on stale mounts
_fs_executor = ThreadPoolExecutor(max_workers=4)

logger = logging.getLogger(__name__)

# ── Config ──
DATA_DIR = '/data'
# Allowed root paths for backup source browsing. Includes legacy /data for
# Docker deployments, plus native host paths for direct installations.
_DEFAULT_BROWSE_ROOTS = ['/data', '/home', '/media', '/run/media', '/mnt', '/opt', '/srv', '/var', '/etc']
BROWSE_ROOTS = [r for r in _DEFAULT_BROWSE_ROOTS if os.path.isdir(r)]

def _effective_browse_roots():
    """Return allowed browse roots for the current request.
    Admin users get all BROWSE_ROOTS; regular users get only their own home."""
    if getattr(g, 'role', None) == 'admin':
        return BROWSE_ROOTS
    username = getattr(g, 'username', None)
    if username:
        home = f'/home/{username}'
        if os.path.isdir(home):
            return [home]
    return []
BACKUP_DIR = os.environ.get('BACKUP_DIR', app_path('backups'))
PATHS_FILE = data_path('paths.json')
HISTORY_FILE = data_path('history.json')
SSH_CONFIG_FILE = data_path('ssh_configs.json')
SCHEDULE_FILE = data_path('schedule_state.json')
SCHEDULER_LOG = log_path('scheduler.log')

# ── State ──
operation_lock = threading.Lock()
current_operation = None
progress_state = {'last': None, 'timestamp': 0}
_progress_lock = threading.Lock()

# Reference to the SocketIO instance — set by init_backup()
_socketio = None

# Ensure dirs
os.makedirs(data_path(), exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)
os.makedirs(os.path.dirname(SCHEDULER_LOG), exist_ok=True)


def init_backup(socketio):
    """Initialize backup module — called from main app after socketio is created."""
    global _socketio
    _socketio = socketio
    init_profiles_db()
    # Start progress worker
    threading.Thread(target=_progress_worker, daemon=True).start()
    # Start scheduler as socketio background task
    socketio.start_background_task(_scheduler_worker)


def get_backup_notifications():
    """Return backup-related notification items for the global notification panel."""
    notifs = []

    # 1) In-progress backup
    with operation_lock:
        op = current_operation
    with _progress_lock:
        prog = progress_state.get('last')
        prog_ts = progress_state.get('timestamp', time.time())
    if op and prog:
        pct = prog.get('overall_percent', prog.get('percent', 0))
        stage = prog.get('stage', 'archive')
        stage_label = 'Transfer' if stage == 'transfer' else 'Archiwizacja'
        notifs.append({
            'type': 'progress',
            'title': 'Backup w toku',
            'message': f'{stage_label}: {round(pct)}%',
            'time': prog_ts,
            'action': {'app': 'backup', 'tab': 'backup'}
        })

    # 2) Recent completed/failed backups from history (last 24h, max 5)
    try:
        history = load_history()
        cutoff = time.time() - 86400
        count = 0
        for entry in history:
            if count >= 5:
                break
            ts = entry.get('timestamp', '')
            try:
                entry_time = datetime.fromisoformat(ts).timestamp()
            except Exception:
                continue
            if entry_time < cutoff:
                break
            status = entry.get('status', '')
            if status == 'completed':
                size_mb = round(entry.get('size', 0) / (1024 * 1024), 1)
                duration = entry.get('duration', 0)
                notifs.append({
                    'type': 'success',
                    'title': 'Backup zakończony',
                    'message': f'{entry.get("archive_file", "?")} — {size_mb} MB, {round(duration)}s',
                    'time': entry_time,
                    'action': {'app': 'backup', 'tab': 'history'}
                })
            elif status == 'failed':
                notifs.append({
                    'type': 'error',
                    'title': 'Backup nieudany',
                    'message': entry.get('error', 'Nieznany błąd'),
                    'time': entry_time,
                    'action': {'app': 'backup', 'tab': 'history'}
                })
            count += 1
    except Exception:
        pass

    return notifs


# ── Helpers ──

def _fs_call_with_timeout(func, *args, timeout=3):
    future = _fs_executor.submit(func, *args)
    try:
        return future.result(timeout=timeout)
    except FuturesTimeoutError:
        future.cancel()
        raise TimeoutError(f"Filesystem operation timed out after {timeout}s")


def _emit(event, data):
    sio_emit(_socketio, event, data)


def emit_log(message, log_type='info'):
    _emit('backup_log', {'type': log_type, 'message': message})
    logger.info(f"[backup][{log_type}] {message}")


def emit_progress(operation, percent, current_file, files_done, total_files, bytes_done, total_bytes, eta=None, stage='archive', stage_percent=0, overall_percent=None):
    if overall_percent is None:
        overall_percent = percent
    progress = {
        'operation': operation,
        'percent': round(overall_percent, 1),
        'current_file': current_file,
        'files_done': files_done,
        'total_files': total_files,
        'bytes_done': bytes_done,
        'total_bytes': total_bytes,
        'eta': eta,
        'stage': stage,
        'stage_percent': round(stage_percent, 1),
        'overall_percent': round(overall_percent, 1)
    }
    _emit('backup_progress', progress)
    with _progress_lock:
        progress_state['last'] = progress
        progress_state['timestamp'] = time.time()


def _progress_worker():
    """Periodically re-emit last progress so reconnecting clients see it."""
    while True:
        time.sleep(10)
        with operation_lock:
            op = current_operation
        with _progress_lock:
            last = progress_state['last']
        if op and last:
            _emit('backup_progress', last)


# ── File helpers ──

from utils import load_json, save_json, sio_emit, list_directory as _list_dir, fmt_bytes


def load_paths():
    return load_json(PATHS_FILE, [])

def save_paths(paths):
    save_json(PATHS_FILE, paths)

def load_history():
    return load_json(HISTORY_FILE, [])

def _atomic_save_json(filepath, data):
    """Atomic JSON write with file locking."""
    tmp = filepath + '.tmp'
    with open(tmp, 'w') as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, filepath)

def save_history(history):
    _atomic_save_json(HISTORY_FILE, history)

def _strip_secrets_from_entry(entry):
    """Remove passwords/secrets from a history entry before persisting."""
    entry = dict(entry)
    if isinstance(entry.get('destination'), dict):
        dest = dict(entry['destination'])
        if isinstance(dest.get('config'), dict):
            cfg = {k: v for k, v in dest['config'].items() if k != 'password'}
            cfg['has_password'] = bool(dest['config'].get('password'))
            dest['config'] = cfg
        entry['destination'] = dest
    return entry

def add_to_history(entry):
    history = load_history()
    history.insert(0, _strip_secrets_from_entry(entry))
    history = history[:100]
    save_history(history)

def load_ssh_configs():
    """Load SSH configs, decrypting passwords transparently."""
    configs = load_json(SSH_CONFIG_FILE, [])
    for c in configs:
        if c.get('password'):
            c['password'] = decrypt_secret(c['password'])
    return configs

def save_ssh_configs(configs):
    """Save SSH configs, encrypting passwords before writing to disk."""
    encrypted = []
    for c in configs:
        cc = dict(c)
        if cc.get('password'):
            cc['password'] = encrypt_secret(cc['password'])
        encrypted.append(cc)
    _atomic_save_json(SSH_CONFIG_FILE, encrypted)

def load_schedule_state():
    return load_json(SCHEDULE_FILE, {})

def save_schedule_state(state):
    save_json(SCHEDULE_FILE, state)


def load_profiles():
    conn = get_db_connection()
    rows = conn.execute('SELECT * FROM profiles').fetchall()
    profiles = []
    for row in rows:
        sched = row['schedule']
        try:
            sched = json.loads(sched) if sched else None
        except Exception:
            pass
        profiles.append({
            'id': str(row['id']),
            'name': row['name'],
            'paths': json.loads(row['paths']),
            'destination': json.loads(row['destination']) if row['destination'] else None,
            'schedule': sched,
            'options': row['options'],
            'retention': row['retention'] if row['retention'] else 0,
            'incremental': bool(row['incremental']) if row['incremental'] else False,
        })
    conn.close()
    return profiles


# ── USB detection ──

BACKUP_EXCLUDE_BASENAMES = {'backups', 'backup', 'cache', '.cache'}

def _is_excluded(dirpath):
    return os.path.basename(dirpath).lower() in BACKUP_EXCLUDE_BASENAMES

def _add_drive(drives_list, name, path):
    if any(d['path'] == path for d in drives_list):
        return
    try:
        stat = _fs_call_with_timeout(os.statvfs, path, timeout=3)
        total = stat.f_blocks * stat.f_frsize
        free = stat.f_bfree * stat.f_frsize
        used = total - free
        if total < 1024 * 1024:
            return
        drives_list.append({
            'name': f"USB: {name}",
            'path': path,
            'total': total,
            'used': used,
            'free': free,
            'percent_used': round((used / total * 100) if total > 0 else 0, 1)
        })
    except (OSError, PermissionError, TimeoutError):
        pass


def detect_usb_drives():
    usb_drives = []
    mount_bases = {
        '/data/media': True,
        '/data/run_media': True,
        '/data/mnt': False,
        '/media': True,
        '/run/media': True,
        '/mnt': False,
    }
    for base_path, has_user_dirs in mount_bases.items():
        try:
            if not _fs_call_with_timeout(os.path.exists, base_path, timeout=2):
                continue
            entries = _fs_call_with_timeout(os.listdir, base_path, timeout=3)
            for item in entries:
                item_path = os.path.join(base_path, item)
                try:
                    if not _fs_call_with_timeout(os.path.isdir, item_path, timeout=2):
                        continue
                except (TimeoutError, OSError):
                    continue
                if has_user_dirs:
                    try:
                        for drive in _fs_call_with_timeout(os.listdir, item_path, timeout=3):
                            drive_path = os.path.join(item_path, drive)
                            try:
                                if _fs_call_with_timeout(os.path.isdir, drive_path, timeout=2):
                                    _add_drive(usb_drives, drive, drive_path)
                            except (TimeoutError, OSError):
                                continue
                    except (PermissionError, TimeoutError, OSError):
                        continue
                else:
                    _add_drive(usb_drives, item, item_path)
        except (PermissionError, TimeoutError, OSError):
            continue

    # Try lsblk
    try:
        result = subprocess.run(
            ['lsblk', '-J', '-o', 'NAME,SIZE,TYPE,MOUNTPOINT,TRAN'],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            for device in data.get('blockdevices', []):
                if device.get('tran') == 'usb':
                    for child in device.get('children', []):
                        mp = child.get('mountpoint')
                        if mp:
                            cp = None
                            if mp.startswith('/media/'):
                                cp = '/data/media/' + mp[len('/media/'):]
                            elif mp.startswith('/run/media/'):
                                cp = '/data/run_media/' + mp[len('/run/media/'):]
                            elif mp.startswith('/mnt/'):
                                cp = '/data/mnt/' + mp[len('/mnt/'):] # legacy
                            if cp and os.path.exists(cp):
                                _add_drive(usb_drives, child.get('name', mp.split('/')[-1]), cp)
                            elif not cp and os.path.exists(mp):
                                _add_drive(usb_drives, mp.split('/')[-1], mp)
                            elif cp and not os.path.exists(cp) and os.path.exists(mp):
                                _add_drive(usb_drives, mp.split('/')[-1], mp)
    except Exception:
        pass

    return usb_drives


# ── SSH ── (uses shared ssh_utils)

def test_ssh_connection(host, port, username, password=None, key_path=None, remote_path=None):
    if not HAS_SSH:
        return False, "paramiko not installed"
    try:
        ssh = _get_ssh_client(host, port, username, password=password, key_path=key_path)
        disk_info = _ssh_remote_disk_info(ssh) or {}
        remote_home = _ssh_resolve_home(ssh)
        if remote_home:
            disk_info['remote_home'] = remote_home
        if remote_path:
            resolved = remote_path.replace('~', remote_home or '/tmp').replace('$HOME', remote_home or '/tmp')
            writable, _ = _ssh_ensure_writable_dir(ssh, resolved)
            disk_info['path_writable'] = writable
            disk_info['resolved_path'] = resolved
            if not writable and remote_home:
                fallback = f'{remote_home}/backups'
                fb_ok, _ = _ssh_ensure_writable_dir(ssh, fallback)
                if fb_ok:
                    disk_info['suggested_path'] = fallback
        ssh.close()
        return True, disk_info
    except Exception as e:
        return False, str(e)


# ── Size helpers ──

def get_size(path, exclude=False):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    root_path = os.path.normpath(path)
    for dirpath, dirnames, filenames in os.walk(path):
        if exclude and os.path.normpath(dirpath) != root_path and _is_excluded(dirpath):
            dirnames.clear()
            continue
        for f in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, f))
            except Exception:
                pass
    return total


def count_files(path, exclude=False):
    if os.path.isfile(path):
        return 1
    count = 0
    root_path = os.path.normpath(path)
    for dirpath, dirnames, filenames in os.walk(path):
        if exclude and os.path.normpath(dirpath) != root_path and _is_excluded(dirpath):
            dirnames.clear()
            continue
        count += len(filenames)
    return count


# ── Transfer ──


def transfer_to_ssh(backup_path, ssh_config, backup_filename):
    if not HAS_SSH:
        return False, "paramiko not installed"
    try:
        emit_log(f"Kopiowanie do SSH: {ssh_config['host']}", 'info')
        source_size = os.path.getsize(backup_path)

        def progress_callback(filename, size, sent):
            percent = (sent / size * 100) if size > 0 else 0
            emit_progress(
                operation=f"Kopiowanie do SSH: {ssh_config['host']}",
                percent=50 + percent * 0.5,
                current_file=filename.decode() if isinstance(filename, bytes) else filename,
                files_done=1 if sent >= size else 0,
                total_files=1, bytes_done=sent, total_bytes=size,
                stage='transfer', stage_percent=percent,
                overall_percent=50 + percent * 0.5
            )

        ssh = _get_ssh_client(ssh_config['host'], ssh_config.get('port', 22),
                              ssh_config['username'],
                              password=ssh_config.get('password'),
                              key_path=ssh_config.get('key_path'), timeout=30)
        remote_path = ssh_config.get('remote_path', '/tmp')
        remote_file = f"{remote_path}/{backup_filename}"
        ssh.exec_command(f"mkdir -p {remote_path}")
        with SCPClient(ssh.get_transport(), progress=progress_callback) as scp:
            scp.put(backup_path, remote_file)
        ssh.close()
        emit_log(f"Skopiowano do SSH: {remote_file}", 'success')
        return True, remote_file
    except Exception as e:
        return False, str(e)


# ── Retention ──

def apply_retention(retention, backup_dir, destination=None, profile_name=None):
    if not retention or retention <= 0:
        return
    try:
        local_backups = sorted(
            [f for f in os.listdir(backup_dir) if f.startswith('backup_') and f.endswith('.tar.gz')],
            key=lambda f: os.path.getmtime(os.path.join(backup_dir, f)), reverse=True
        )
        # Never delete the latest level-0 (full) backup — incrementals depend on it
        protected_full = None
        for f in local_backups:
            if '_incr' not in f:
                protected_full = f
                break
        if len(local_backups) > retention:
            for f in local_backups[retention:]:
                if f == protected_full:
                    continue
                try:
                    os.remove(os.path.join(backup_dir, f))
                    emit_log(f"Rotacja: usunięto {f}", 'info')
                except Exception as e:
                    emit_log(f"Rotacja: błąd usuwania {f}: {e}", 'warning')

        if destination and destination.get('type') == 'usb' and destination.get('path'):
            usb_path = destination['path']
            if os.path.isdir(usb_path):
                usb_backups = sorted(
                    [f for f in os.listdir(usb_path) if f.startswith('backup_') and f.endswith('.tar.gz')],
                    key=lambda f: os.path.getmtime(os.path.join(usb_path, f)), reverse=True
                )
                # Protect the latest full backup on USB too
                usb_protected_full = None
                for f in usb_backups:
                    if '_incr' not in f:
                        usb_protected_full = f
                        break
                if len(usb_backups) > retention:
                    for f in usb_backups[retention:]:
                        if f == usb_protected_full:
                            continue
                        try:
                            os.remove(os.path.join(usb_path, f))
                        except Exception:
                            pass
    except Exception as e:
        emit_log(f"Błąd rotacji: {e}", 'warning')


# ── Incremental chain detection ──

def detect_incremental_chain(selected_file, search_dir=None):
    if '_incr' not in selected_file:
        return [selected_file]
    d = search_dir or BACKUP_DIR
    all_backups = sorted(
        [f for f in os.listdir(d) if f.startswith('backup_') and f.endswith('.tar.gz')]
    )
    full_backup = None
    for f in all_backups:
        if f >= selected_file:
            break
        if '_incr' not in f:
            full_backup = f
    if not full_backup:
        return [selected_file]
    chain = [full_backup]
    for f in all_backups:
        if f <= full_backup:
            continue
        if f > selected_file:
            break
        if '_incr' in f:
            chain.append(f)
    return chain


# ── Backup ──

def run_backup(paths, destination=None, profile_name=None, retention=0, incremental=False):
    global current_operation
    # USB: write tar directly to destination (no local copy needed)
    # SSH: still needs local copy + transfer
    direct_to_usb = destination and destination.get('type') == 'usb' and destination.get('path')
    has_transfer = destination and destination.get('type') == 'ssh'
    archive_weight = 50 if has_transfer else 100
    history_entry = {
        'id': datetime.now().strftime("%Y%m%d%H%M%S"),
        'timestamp': datetime.now().isoformat(),
        'paths': paths, 'destination': destination,
        'status': 'in_progress', 'archive_file': None,
        'final_location': None, 'size': 0,
        'files_count': 0, 'duration': 0, 'error': None
    }
    start_time = time.time()
    try:
        mode_label = "przyrostowy" if incremental else "pełny"
        emit_log(f"Backup {mode_label} — {len(paths)} ścieżek...", 'info')

        # Validate paths before starting
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            raise Exception(f"Ścieżki źródłowe nie istnieją: {', '.join(missing)}")

        if direct_to_usb:
            dest_dir = destination['path']
            if not os.path.isdir(dest_dir):
                raise Exception(f"Katalog docelowy nie istnieje: {dest_dir}")

        total_bytes = 0
        total_files = 0
        for path in paths:
            if os.path.exists(path):
                total_bytes += get_size(path, exclude=True)
                total_files += count_files(path, exclude=True)
        history_entry['files_count'] = total_files
        emit_log(f"Rozmiar: {total_bytes / (1024**3):.2f} GB, plików: {total_files}", 'info')

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # ── Incremental snapshot handling ──
        # Determine snapshot path first so we know if this will be a true
        # incremental or a level-0 (full) backup before naming the file.
        snapshot_path = None
        is_true_incremental = False
        if incremental and profile_name:
            safe_name = profile_name.replace('/', '_').replace(' ', '_')
            snapshot_path = os.path.join(BACKUP_DIR, f".snapshot_{safe_name}.snar")

            # Safety check: if snapshot exists but no full backup (level-0,
            # without '_incr') is found at the destination, delete the snapshot
            # so tar creates a fresh level-0 backup.  This prevents orphaned
            # incrementals after old archives are cleaned up.
            # NOTE: When a destination (USB/SSH) is configured, we ONLY check
            # the destination — local copies are temporary and deleted after
            # transfer, so their presence is meaningless.
            if os.path.exists(snapshot_path):
                has_full_backup = False
                dest_dir = None
                if destination and destination.get('type') == 'usb' and destination.get('path'):
                    dest_dir = destination['path']
                if dest_dir and os.path.isdir(dest_dir):
                    try:
                        has_full_backup = any(
                            f.startswith('backup_') and f.endswith('.tar.gz') and '_incr' not in f
                            for f in os.listdir(dest_dir)
                        )
                    except Exception:
                        pass
                # Only check local backup dir if there is NO destination configured
                if not has_full_backup and not dest_dir:
                    try:
                        has_full_backup = any(
                            f.startswith('backup_') and f.endswith('.tar.gz') and '_incr' not in f
                            for f in os.listdir(BACKUP_DIR)
                        )
                    except Exception:
                        pass

                if not has_full_backup:
                    os.remove(snapshot_path)
                    emit_log("Brak pełnego backupu bazowego — resetuję snapshot i tworzę nowy pełny backup", 'warning')

            # After the safety check, if snapshot still exists → true incremental
            is_true_incremental = os.path.exists(snapshot_path)

        # Suffix: only add _incr for actual incremental backups, NOT for level-0 (full)
        suffix = "_incr" if is_true_incremental else ""
        backup_filename = f"backup_{timestamp}{suffix}.tar.gz"

        # When USB destination is set, create tar directly on USB (no local copy)
        if direct_to_usb:
            usb_dir = destination['path']
            os.makedirs(usb_dir, exist_ok=True)
            backup_path = os.path.join(usb_dir, backup_filename)
            emit_log(f"Archiwum tworzone bezpośrednio na USB: {usb_dir}", 'info')
        else:
            backup_path = os.path.join(BACKUP_DIR, backup_filename)
        history_entry['archive_file'] = backup_filename

        cmd = ['tar', '-cvzf', backup_path]
        if snapshot_path is not None:
            cmd.append(f'--listed-incremental={snapshot_path}')
            if is_true_incremental:
                emit_log("Backup przyrostowy — archiwizacja tylko zmienionych plików", 'info')
            else:
                emit_log("Pierwszy backup — tworzenie pełnego archiwum z plikiem snapshot", 'info')

        for path in paths:
            if os.path.exists(path):
                stripped = path.lstrip('/')
                for excl in ['backups', 'Backups', 'backup', '.cache', 'cache']:
                    cmd.append(f'--exclude={stripped}/{excl}')
                    cmd.append(f'--exclude={stripped}/*/{excl}')

        for path in paths:
            if os.path.exists(path):
                cmd.extend(['-C', '/', path.lstrip('/')])

        emit_log("Etap 1: Archiwizacja...", 'info')
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        files_done = 0
        bytes_done = 0
        archive_start = time.time()
        last_progress_time = 0
        tar_errors = []

        for line in process.stdout:
            line = line.strip()
            if line:
                files_done += 1
                try:
                    if os.path.exists('/' + line):
                        bytes_done += os.path.getsize('/' + line)
                except Exception:
                    pass
                # Yield to gevent event loop periodically
                if files_done % 50 == 0:
                    gevent.sleep(0)
                percent = (files_done / total_files * 100) if total_files > 0 else (files_done / max(files_done, 1) * 50)
                elapsed = time.time() - archive_start
                eta = (elapsed / percent) * (100 - percent) if percent > 0 else None
                now = time.time()
                if total_files <= 100 or files_done % 10 == 0 or files_done == total_files or (now - last_progress_time) >= 0.5:
                    last_progress_time = now
                    overall = percent * archive_weight / 100
                    emit_progress(
                        operation=f"Archiwizacja: {backup_filename}",
                        percent=overall, current_file=line[-50:] if len(line) > 50 else line,
                        files_done=files_done, total_files=total_files,
                        bytes_done=bytes_done, total_bytes=total_bytes,
                        eta=eta, stage='archive', stage_percent=percent,
                        overall_percent=overall
                    )

        # Capture stderr for error diagnostics
        stderr_output = ''
        try:
            stderr_output = process.stderr.read() or ''
        except Exception:
            pass

        process.wait()
        if process.returncode >= 2:
            err_detail = stderr_output.strip()[:500] if stderr_output.strip() else 'brak szczegółów'
            emit_log(f"tar stderr: {err_detail}", 'error')
            raise Exception(f"Błąd archiwizacji (kod: {process.returncode}): {err_detail}")
        elif process.returncode == 1:
            if stderr_output.strip():
                emit_log(f"Ostrzeżenia tar: {stderr_output.strip()[:300]}", 'warning')
            emit_log("Archiwizacja z ostrzeżeniami", 'warning')

        final_size = os.path.getsize(backup_path) if os.path.exists(backup_path) else 0
        history_entry['size'] = final_size
        emit_log(f"Archiwum: {backup_filename} ({final_size / (1024*1024):.2f} MB)", 'success')

        final_location = backup_path
        if destination:
            if destination['type'] == 'usb':
                # Archive already created directly on USB — no transfer needed
                final_location = backup_path
                emit_log(f"Archiwum zapisane na USB: {backup_path}", 'success')
            elif destination['type'] == 'ssh':
                emit_log("Etap 2: Kopiowanie do SSH...", 'info')
                ok, result = transfer_to_ssh(backup_path, destination['config'], backup_filename)
                if ok:
                    final_location = result
                else:
                    raise Exception(f"SSH: {result}")

        history_entry['final_location'] = final_location
        history_entry['status'] = 'completed'
        history_entry['duration'] = round(time.time() - start_time, 1)
        add_to_history(history_entry)
        with _progress_lock:
            progress_state['last'] = None
        emit_log(f"Backup zakończony: {final_location}", 'success')
        _emit('backup_complete', {'message': f'Backup zakończony: {backup_filename}'})

        if retention and retention > 0:
            # For USB: apply retention on USB directory (archives are there)
            retention_dir = destination['path'] if direct_to_usb else BACKUP_DIR
            apply_retention(retention, retention_dir, destination, profile_name)

        # If transferred to SSH, remove ALL local copies (no local retention)
        # For USB: archive was created directly there, no local copies to clean
        if has_transfer:
            try:
                for f in os.listdir(BACKUP_DIR):
                    if f.startswith('backup_') and f.endswith('.tar.gz'):
                        try:
                            os.remove(os.path.join(BACKUP_DIR, f))
                        except Exception:
                            pass
                emit_log("Kopie lokalne usunięte (backup na dysku zewnętrznym)", 'info')
            except Exception:
                pass

    except Exception as e:
        history_entry['status'] = 'failed'
        history_entry['error'] = str(e)
        history_entry['duration'] = round(time.time() - start_time, 1)
        add_to_history(history_entry)
        with _progress_lock:
            progress_state['last'] = None
        emit_log(f"Błąd backupu: {e}", 'error')
        _emit('backup_error', {'message': str(e)})
    finally:
        with operation_lock:
            current_operation = None


# ── Restore ──

def run_restore(backup_file, target_path=None, archive_dir=None):
    global current_operation
    d = archive_dir or BACKUP_DIR
    try:
        chain = detect_incremental_chain(backup_file, search_dir=d)
        extract_to = target_path if target_path else '/'
        if target_path:
            os.makedirs(target_path, exist_ok=True)

        if len(chain) > 1:
            emit_log(f"Łańcuch przyrostowy — {len(chain)} archiwów", 'info')
        else:
            emit_log(f"Przywracanie z {backup_file}...", 'info')

        if target_path:
            emit_log(f"Cel: {target_path}", 'info')

        total_files = 0
        total_bytes = 0
        for f in chain:
            fpath = os.path.join(d, f)
            if not os.path.exists(fpath):
                raise Exception(f"Brak pliku: {f}")
            total_bytes += os.path.getsize(fpath)
            result = subprocess.run(['tar', '-tzf', fpath], capture_output=True, text=True)
            if result.returncode == 0:
                total_files += len([l for l in result.stdout.strip().split('\n') if l])

        files_done = 0
        start_time = time.time()
        last_progress_time = 0

        for chain_idx, archive_file in enumerate(chain):
            archive_path = os.path.join(d, archive_file)
            cmd = ['tar', '-xvzf', archive_path, '-C', extract_to]
            if '_incr' in archive_file or len(chain) > 1:
                cmd.insert(1, '--listed-incremental=/dev/null')

            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in process.stdout:
                line = line.strip()
                if line:
                    files_done += 1
                    percent = (files_done / total_files * 100) if total_files > 0 else 0
                    elapsed = time.time() - start_time
                    eta = (elapsed / percent) * (100 - percent) if percent > 0 else None
                    now = time.time()
                    if total_files <= 100 or files_done % 10 == 0 or files_done == total_files or (now - last_progress_time) >= 0.5:
                        last_progress_time = now
                        emit_progress(
                            operation=f"Przywracanie: {archive_file}",
                            percent=percent, current_file=line[-60:] if len(line) > 60 else line,
                            files_done=files_done, total_files=total_files,
                            bytes_done=int(total_bytes * percent / 100),
                            total_bytes=total_bytes, eta=eta
                        )

            process.wait()
            if process.returncode >= 2:
                raise Exception(f"Błąd rozpakowywania {archive_file} (kod: {process.returncode})")
            elif process.returncode == 1:
                emit_log(f"Ostrzeżenia przy rozpakowywaniu {archive_file}", 'warning')

        emit_log(f"Przywracanie zakończone pomyślnie! → {target_path or 'oryginalne lokalizacje'}", 'success')
        with _progress_lock:
            progress_state['last'] = None
        _emit('backup_complete', {'message': f'Przywracanie zakończone: {backup_file}'})

    except Exception as e:
        emit_log(f"Błąd przywracania: {e}", 'error')
        with _progress_lock:
            progress_state['last'] = None
        _emit('backup_error', {'message': str(e)})
    finally:
        with operation_lock:
            current_operation = None


# ── Scheduler ──

def log_scheduler(message, level='INFO'):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    logger.info(f"Scheduler: {message}")
    try:
        with open(SCHEDULER_LOG, 'a') as f:
            f.write(f"[{timestamp}] [{level}] {message}\n")
    except Exception:
        pass


def _calc_next_run(schedule, last_run_iso=None):
    try:
        now = datetime.now()
        sched_time = schedule.get('time', '03:00')
        hour, minute = int(sched_time.split(':')[0]), int(sched_time.split(':')[1])
        if schedule.get('type') == 'daily':
            candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= now:
                candidate += timedelta(days=1)
            return candidate.isoformat()
        elif schedule.get('type') == 'weekly':
            days = schedule.get('days', [])
            if not days:
                return None
            for offset in range(8):
                candidate = now + timedelta(days=offset)
                candidate = candidate.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if candidate.weekday() in days and candidate > now:
                    return candidate.isoformat()
        return None
    except Exception:
        return None


def _scheduler_worker():
    """Background scheduler — runs as socketio background task (gevent-friendly)."""
    global current_operation
    log_scheduler("Scheduler started")
    while True:
        try:
            _socketio.sleep(30)
            with operation_lock:
                if current_operation is not None:
                    continue
            profiles = load_profiles()
            state = load_schedule_state()
            now = datetime.now()

            for profile in profiles:
                schedule = None
                if profile.get('schedule'):
                    try:
                        schedule = json.loads(profile['schedule']) if isinstance(profile['schedule'], str) else profile['schedule']
                    except Exception:
                        continue
                if not schedule or schedule.get('type') == 'manual':
                    continue

                profile_id = str(profile['id'])
                last_run_iso = state.get(profile_id, {}).get('last_run')
                sched_time = schedule.get('time', '03:00')
                try:
                    hour, minute = int(sched_time.split(':')[0]), int(sched_time.split(':')[1])
                except Exception:
                    continue

                should_run = False
                if schedule.get('type') == 'daily':
                    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                    if now >= target:
                        if not last_run_iso:
                            should_run = True
                        else:
                            last_run = datetime.fromisoformat(last_run_iso)
                            if last_run.date() < now.date() or (last_run.date() == now.date() and last_run.replace(hour=hour, minute=minute, second=0) < target):
                                should_run = True
                elif schedule.get('type') == 'weekly':
                    days = schedule.get('days', [])
                    if now.weekday() in days:
                        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if now >= target:
                            if not last_run_iso:
                                should_run = True
                            else:
                                last_run = datetime.fromisoformat(last_run_iso)
                                if last_run < target:
                                    should_run = True

                if should_run:
                    with operation_lock:
                        if current_operation is not None:
                            break
                        current_operation = 'backup'
                    log_scheduler(f"BACKUP STARTED — '{profile['name']}'")
                    state[profile_id] = {'last_run': now.isoformat()}
                    save_schedule_state(state)

                    destination = profile.get('destination')
                    if destination and destination.get('type') == 'ssh':
                        ssh_id = destination.get('server_id')
                        if ssh_id:
                            configs = load_ssh_configs()
                            ssh_cfg = next((c for c in configs if c.get('id') == ssh_id), None)
                            if ssh_cfg:
                                destination['config'] = ssh_cfg

                    retention = profile.get('retention', 0)
                    incremental = profile.get('incremental', False)
                    emit_log(f"Zaplanowany backup: {profile['name']}", 'info')
                    _socketio.start_background_task(_run_scheduled_backup, profile, destination, retention, incremental)
                    break
        except Exception as e:
            log_scheduler(f"Error: {e}", 'ERROR')


def _run_scheduled_backup(profile, destination, retention, incremental):
    """Wrapper for run_backup that logs scheduler outcome."""
    profile_name = profile['name']
    start = time.time()
    try:
        run_backup(profile['paths'], destination, profile_name, retention, incremental)
        duration = round(time.time() - start, 1)
        log_scheduler(f"BACKUP COMPLETED - profile '{profile_name}' in {duration}s")
    except Exception as e:
        duration = round(time.time() - start, 1)
        log_scheduler(f"BACKUP FAILED - profile '{profile_name}' after {duration}s: {e}", 'ERROR')


# ═══════════════════════════════════════════════════════════
#  API Routes
# ═══════════════════════════════════════════════════════════

@backup_bp.route('/browse/roots')
def browse_roots():
    roots = []
    for r in _effective_browse_roots():
        try:
            stat = os.statvfs(r)
            total = stat.f_blocks * stat.f_frsize
            free = stat.f_bfree * stat.f_frsize
            roots.append({'path': r, 'name': r, 'total': total, 'free': free})
        except OSError:
            roots.append({'path': r, 'name': r, 'total': None, 'free': None})
    return jsonify({'roots': roots})

@backup_bp.route('/browse')
def browse_directory():
    allowed = _effective_browse_roots()
    if not allowed:
        return jsonify({'error': 'Brak dostępu'}), 403
    path = request.args.get('path', allowed[0])
    if not any(path.startswith(r) for r in allowed):
        path = allowed[0]
    items, err = _list_dir(path, allowed_prefix=allowed, timeout=5)
    if err:
        code = 404 if 'Not a directory' in err else 400 if 'outside' in err else 403
        return jsonify({'error': err}), code
    # Convert to camelCase keys + add sizeDisplay
    out = []
    for i in items:
        sz = i['size']
        if i['is_dir']:
            sd = 'folder'
        elif sz < 1024:
            sd = f'{sz} B'
        elif sz < 1048576:
            sd = f'{sz / 1024:.1f} KB'
        elif sz < 1073741824:
            sd = f'{sz / 1048576:.1f} MB'
        else:
            sd = f'{sz / 1073741824:.2f} GB'
        out.append({
            'name': i['name'], 'path': i['path'], 'isDir': i['is_dir'],
            'size': i['size'] if not i['is_dir'] else None,
            'sizeDisplay': sd, 'modified': i['modified'],
        })
    parent_path = os.path.dirname(path) if path not in allowed else None
    if parent_path and not any(parent_path.startswith(r) for r in allowed):
        parent_path = None
    return jsonify({'currentPath': path, 'parentPath': parent_path, 'items': out})


@backup_bp.route('/browse/mkdir', methods=['POST'])
def browse_mkdir():
    allowed = _effective_browse_roots()
    data = request.json
    parent = data.get('path', '')
    name = data.get('name', '').strip()
    if not parent or not name:
        return jsonify({'error': 'Ścieżka i nazwa wymagane'}), 400
    if not any(parent.startswith(r) for r in allowed):
        return jsonify({'error': 'Nieprawidłowa ścieżka'}), 400
    if '/' in name or '..' in name:
        return jsonify({'error': 'Nieprawidłowa nazwa'}), 400
    new_path = os.path.join(parent, name)
    if os.path.exists(new_path):
        return jsonify({'error': 'Folder już istnieje'}), 400
    try:
        os.makedirs(new_path)
        return jsonify({'success': True, 'path': new_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/paths', methods=['GET'])
def get_paths():
    return jsonify({'paths': load_paths()})

@backup_bp.route('/paths', methods=['POST'])
def add_path():
    data = request.json
    path = data.get('path', '').strip()
    if not path:
        return jsonify({'error': 'Ścieżka jest wymagana'}), 400
    if not os.path.exists(path):
        return jsonify({'error': f'Ścieżka nie istnieje: {path}'}), 400
    paths = load_paths()
    if path not in paths:
        paths.append(path)
        save_paths(paths)
    return jsonify({'success': True, 'paths': paths})

@backup_bp.route('/paths', methods=['DELETE'])
def remove_path():
    data = request.json
    path = data.get('path', '').strip()
    paths = load_paths()
    if path in paths:
        paths.remove(path)
        save_paths(paths)
    return jsonify({'success': True, 'paths': paths})


@backup_bp.route('/backups')
def list_backups():
    backups = []
    scanned_dirs = set()

    # Build mapping: destination_path -> profile info
    profiles = []
    dir_to_profile = {}
    try:
        profiles = load_profiles()
        for profile in profiles:
            dest = profile.get('destination')
            if not dest:
                continue
            if dest.get('type') == 'usb' and dest.get('path'):
                dir_to_profile[dest['path']] = {
                    'profile_id': profile.get('id'),
                    'profile_name': profile.get('name', '')
                }
            elif dest.get('type') == 'local' or not dest.get('type'):
                dir_to_profile[BACKUP_DIR] = {
                    'profile_id': profile.get('id'),
                    'profile_name': profile.get('name', '')
                }
    except Exception:
        pass

    def scan_dir(directory, location_label):
        if directory in scanned_dirs:
            return
        scanned_dirs.add(directory)
        if not os.path.isdir(directory):
            return
        prof = dir_to_profile.get(directory, {})
        try:
            for item in _fs_call_with_timeout(os.listdir, directory, timeout=5):
                if item.endswith('.tar.gz') and item.startswith('backup_'):
                    fpath = os.path.join(directory, item)
                    try:
                        st = _fs_call_with_timeout(os.stat, fpath, timeout=3)
                        entry = {
                            'name': item, 'size': st.st_size,
                            'modified': datetime.fromtimestamp(st.st_mtime).isoformat(),
                            'location': location_label, 'path': fpath
                        }
                        if prof:
                            entry['profile_id'] = prof['profile_id']
                            entry['profile_name'] = prof['profile_name']
                        backups.append(entry)
                    except Exception:
                        pass
        except Exception:
            pass

    # 1. Local backups
    scan_dir(BACKUP_DIR, 'Lokalnie')

    # 2. Scan destinations from all profiles
    for profile in profiles:
        dest = profile.get('destination')
        if not dest:
            continue
        if dest.get('type') == 'usb' and dest.get('path'):
            scan_dir(dest['path'], f"USB: {dest['path']}")

    backups.sort(key=lambda x: x['modified'], reverse=True)
    return jsonify({'backups': backups})

@backup_bp.route('/backups/<filename>', methods=['DELETE'])
def delete_backup(filename):
    # Support deleting from a specific path (passed as query param)
    custom_path = request.args.get('path')
    if custom_path and os.path.exists(custom_path) and custom_path.endswith('.tar.gz'):
        os.remove(custom_path)
        return jsonify({'success': True})
    bp = os.path.join(BACKUP_DIR, filename)
    if os.path.exists(bp):
        os.remove(bp)
        return jsonify({'success': True})
    return jsonify({'error': 'Backup nie istnieje'}), 404


@backup_bp.route('/backup', methods=['POST'])
def start_backup():
    global current_operation
    with operation_lock:
        if current_operation is not None:
            return jsonify({'error': 'Inna operacja jest w toku'}), 400
        current_operation = 'backup'

    data = request.json
    paths = data.get('paths', [])
    destination = data.get('destination')
    retention = data.get('retention', 0)
    incremental = data.get('incremental', False)

    if not paths:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'Wybierz przynajmniej jedną ścieżkę'}), 400

    valid_paths = [p for p in paths if os.path.exists(p)]
    if not valid_paths:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'Żadna ścieżka nie istnieje'}), 400

    if destination:
        if destination['type'] == 'usb':
            if not os.path.exists(destination.get('path', '')):
                with operation_lock:
                    current_operation = None
                return jsonify({'error': 'USB nie jest dostępne'}), 400
        elif destination['type'] == 'ssh':
            ssh_id = destination.get('server_id')
            if ssh_id:
                configs = load_ssh_configs()
                ssh_cfg = next((c for c in configs if c.get('id') == ssh_id), None)
                if not ssh_cfg:
                    with operation_lock:
                        current_operation = None
                    return jsonify({'error': 'Serwer SSH nie znaleziony'}), 400
                destination['config'] = ssh_cfg

    _socketio.start_background_task(run_backup, valid_paths, destination, retention=retention, incremental=incremental)
    return jsonify({'success': True, 'message': 'Backup rozpoczęty'})


@backup_bp.route('/backup-preview/<filename>')
def backup_preview(filename):
    custom_path = request.args.get('path')
    if custom_path and os.path.isfile(custom_path) and custom_path.endswith('.tar.gz'):
        bp = custom_path
        archive_dir = os.path.dirname(custom_path)
    else:
        bp = os.path.join(BACKUP_DIR, filename)
        archive_dir = BACKUP_DIR
    if not os.path.exists(bp):
        return jsonify({'error': 'Plik nie istnieje'}), 404
    st = os.stat(bp)
    result = subprocess.run(['tar', '-tzf', bp], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return jsonify({'error': 'Nie można odczytać archiwum'}), 500
    all_files = [l for l in result.stdout.strip().split('\n') if l]
    top_dirs = {}
    for f in all_files:
        top = f.split('/')[0]
        top_dirs[top] = top_dirs.get(top, 0) + 1
    chain = detect_incremental_chain(filename, search_dir=archive_dir)
    chain_info = []
    for cf in chain:
        cp = os.path.join(archive_dir, cf)
        if os.path.exists(cp):
            chain_info.append({'name': cf, 'size': os.path.getsize(cp), 'is_full': '_incr' not in cf})
    return jsonify({
        'filename': filename, 'size': st.st_size,
        'modified': datetime.fromtimestamp(st.st_mtime).isoformat(),
        'total_files': len(all_files), 'is_incremental': '_incr' in filename,
        'chain': chain_info, 'top_dirs': top_dirs,
        'files': all_files[:200], 'truncated': len(all_files) > 200
    })


@backup_bp.route('/restore', methods=['POST'])
def start_restore():
    global current_operation
    with operation_lock:
        if current_operation is not None:
            return jsonify({'error': 'Inna operacja jest w toku'}), 400
        current_operation = 'restore'

    data = request.json
    backup_file = data.get('backup_file', '')
    backup_path = data.get('backup_path', '')
    target_path = data.get('target_path', '')
    if not backup_file:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'Wybierz plik backupu'}), 400
    # Resolve archive location: prefer explicit path, fallback to BACKUP_DIR
    if backup_path and os.path.isfile(backup_path) and backup_path.endswith('.tar.gz'):
        bp = backup_path
        archive_dir = os.path.dirname(backup_path)
    else:
        bp = os.path.join(BACKUP_DIR, backup_file)
        archive_dir = BACKUP_DIR
    if not os.path.exists(bp):
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'Plik nie istnieje'}), 400
    restore_target = target_path or None
    if restore_target:
        try:
            os.makedirs(restore_target, exist_ok=True)
        except Exception as e:
            with operation_lock:
                current_operation = None
            return jsonify({'error': f'Nie można utworzyć katalogu: {e}'}), 400

    _socketio.start_background_task(run_restore, backup_file, restore_target, archive_dir)
    return jsonify({'success': True, 'message': 'Przywracanie rozpoczęte'})


@backup_bp.route('/status')
def get_status():
    with operation_lock:
        return jsonify({'busy': current_operation is not None, 'operation': current_operation})

@backup_bp.route('/progress')
def get_progress():
    with _progress_lock:
        return jsonify({'progress': progress_state.get('last')})

@backup_bp.route('/usb-drives')
def get_usb_drives():
    return jsonify({'drives': detect_usb_drives()})

@backup_bp.route('/usb-browse')
def browse_usb():
    path = request.args.get('path', '')
    if not path:
        return jsonify({'error': 'Ścieżka jest wymagana'}), 400
    allowed = ['/data/media', '/data/run_media', '/data/mnt', '/media', '/run/media', '/mnt']
    items, err = _list_dir(path, allowed_prefix=allowed, dirs_only=True, timeout=5)
    if err:
        code = 400 if 'outside' in err else 404 if 'Not a dir' in err else 403
        return jsonify({'error': err}), code
    # Convert to camelCase
    out = [{'name': i['name'], 'path': i['path'], 'isDir': True} for i in items]
    parent = os.path.dirname(path)
    if not any(parent.startswith(p) for p in allowed):
        parent = None
    usb_drives = detect_usb_drives()
    usb_roots = set(d['path'] for d in usb_drives)
    if path in usb_roots:
        parent = None
    return jsonify({'currentPath': path, 'parentPath': parent, 'items': out})

@backup_bp.route('/usb-mkdir', methods=['POST'])
def create_usb_folder():
    data = request.json
    parent = data.get('path', '')
    name = data.get('name', '').strip()
    if not parent or not name:
        return jsonify({'error': 'Ścieżka i nazwa wymagane'}), 400
    allowed = ['/data/media', '/data/run_media', '/data/mnt', '/media', '/run/media', '/mnt']
    if not any(parent.startswith(p) for p in allowed):
        return jsonify({'error': 'Nieprawidłowa ścieżka'}), 400
    if '/' in name or '..' in name:
        return jsonify({'error': 'Nieprawidłowa nazwa'}), 400
    new_path = os.path.join(parent, name)
    if os.path.exists(new_path):
        return jsonify({'error': 'Folder istnieje'}), 400
    try:
        os.makedirs(new_path)
        return jsonify({'success': True, 'path': new_path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/ssh-servers', methods=['GET'])
def get_ssh_servers():
    configs = load_ssh_configs()
    safe = [{k: v for k, v in c.items() if k != 'password'} | {'has_password': bool(c.get('password'))} for c in configs]
    return jsonify({'servers': safe})

@backup_bp.route('/ssh-servers', methods=['POST'])
def add_ssh_server():
    data = request.json
    for field in ['name', 'host', 'username']:
        if not data.get(field):
            return jsonify({'error': f'{field} wymagane'}), 400
    configs = load_ssh_configs()
    # Prevent duplicates (same host + username)
    for existing in configs:
        if existing.get('host') == data['host'] and existing.get('username') == data['username']:
            # Update existing instead of adding duplicate
            existing['name'] = data['name']
            existing['port'] = int(data.get('port', 22))
            existing['password'] = data.get('password', '')
            existing['key_path'] = data.get('key_path', '')
            existing['remote_path'] = data.get('remote_path', '~/backups')
            save_ssh_configs(configs)
            return jsonify({'success': True, 'server': {k: v for k, v in existing.items() if k != 'password'}, 'updated': True})
    new = {
        'id': datetime.now().strftime("%Y%m%d%H%M%S"),
        'name': data['name'], 'host': data['host'],
        'port': int(data.get('port', 22)), 'username': data['username'],
        'password': data.get('password', ''), 'key_path': data.get('key_path', ''),
        'remote_path': data.get('remote_path', '~/backups')
    }
    configs.append(new)
    save_ssh_configs(configs)
    return jsonify({'success': True, 'server': {k: v for k, v in new.items() if k != 'password'}})

@backup_bp.route('/ssh-servers/<server_id>', methods=['DELETE'])
def delete_ssh_server(server_id):
    configs = [c for c in load_ssh_configs() if c.get('id') != server_id]
    save_ssh_configs(configs)
    return jsonify({'success': True})

@backup_bp.route('/ssh-servers/test', methods=['POST'])
def test_ssh():
    data = request.json or {}
    # If called with just 'id', load full server config
    server_id = data.get('id')
    if server_id:
        configs = load_ssh_configs()
        srv = next((c for c in configs if c.get('id') == server_id), None)
        if not srv:
            return jsonify({'success': False, 'error': 'Serwer nie znaleziony'}), 404
        host = srv.get('host')
        port = int(srv.get('port', 22))
        username = srv.get('username')
        password = srv.get('password')
        key_path = srv.get('key_path')
        remote_path = srv.get('remote_path')
    else:
        host = data.get('host')
        port = int(data.get('port', 22))
        username = data.get('username')
        password = data.get('password')
        key_path = data.get('key_path')
        remote_path = data.get('remote_path')

    if not host or not username:
        return jsonify({'success': False, 'error': 'Brak hosta lub użytkownika'}), 400

    ok, result = test_ssh_connection(host, port, username, password, key_path, remote_path)
    if ok:
        return jsonify({'success': True, 'name': data.get('name') or (srv.get('name') if server_id else ''), 'disk_info': result})
    return jsonify({'success': False, 'error': result}), 400


@backup_bp.route('/history')
def get_history_route():
    return jsonify({'history': load_history()})

@backup_bp.route('/history/<entry_id>', methods=['DELETE'])
def delete_history_entry(entry_id):
    history = [h for h in load_history() if h.get('id') != entry_id]
    save_history(history)
    return jsonify({'success': True})


@backup_bp.route('/profiles', methods=['GET'])
def get_profiles():
    return jsonify({'profiles': load_profiles()})

@backup_bp.route('/profiles', methods=['POST'])
def create_profile():
    data = request.json
    if not data.get('name'):
        return jsonify({'error': 'Nazwa wymagana'}), 400
    if not data.get('paths'):
        return jsonify({'error': 'Ścieżki wymagane'}), 400
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('INSERT INTO profiles (name, paths, destination, schedule, options, retention, incremental) VALUES (?, ?, ?, ?, ?, ?, ?)', (
        data['name'], json.dumps(data['paths']),
        json.dumps(data.get('destination')) if data.get('destination') else None,
        json.dumps(data['schedule']) if data.get('schedule') else None,
        data.get('options'), data.get('retention', 0),
        1 if data.get('incremental') else 0
    ))
    conn.commit()
    pid = c.lastrowid
    conn.close()
    return jsonify({'success': True, 'profile': {'id': str(pid), 'name': data['name'], 'paths': data['paths'], 'destination': data.get('destination'), 'schedule': data.get('schedule'), 'retention': data.get('retention', 0), 'incremental': bool(data.get('incremental'))}})


@backup_bp.route('/profiles/export', methods=['GET'])
def export_profiles():
    """Export all profiles as JSON for download."""
    profiles = load_profiles()
    export_data = {
        'version': 1,
        'exported_at': datetime.now().isoformat(),
        'profiles': []
    }
    for p in profiles:
        export_data['profiles'].append({
            'name': p['name'],
            'paths': p['paths'],
            'destination': p['destination'],
            'schedule': p['schedule'],
            'options': p.get('options'),
            'retention': p.get('retention', 0),
            'incremental': p.get('incremental', False),
        })
    return jsonify(export_data)


@backup_bp.route('/profiles/import', methods=['POST'])
def import_profiles():
    """Import profiles from JSON. Supports both file upload and JSON body."""
    try:
        # Support file upload
        if request.files and 'file' in request.files:
            file = request.files['file']
            import_data = json.loads(file.read().decode('utf-8'))
        else:
            import_data = request.json

        if not import_data:
            return jsonify({'error': 'Brak danych do importu'}), 400

        # Support both wrapped format (with 'profiles' key) and raw array
        if isinstance(import_data, list):
            profiles_to_import = import_data
        elif isinstance(import_data, dict):
            profiles_to_import = import_data.get('profiles', [])
        else:
            return jsonify({'error': 'Nieprawidłowy format danych'}), 400

        if not profiles_to_import:
            return jsonify({'error': 'Brak profili do importu'}), 400

        imported = 0
        skipped = 0
        errors = []
        existing = load_profiles()
        existing_names = {p['name'] for p in existing}

        conn = get_db_connection()
        c = conn.cursor()
        for idx, p in enumerate(profiles_to_import):
            try:
                name = p.get('name', '').strip()
                paths = p.get('paths', [])
                if not name:
                    errors.append(f'Profil #{idx+1}: brak nazwy')
                    skipped += 1
                    continue
                if not paths:
                    errors.append(f'Profil "{name}": brak ścieżek')
                    skipped += 1
                    continue

                # Auto-rename duplicates
                original_name = name
                counter = 1
                while name in existing_names:
                    name = f"{original_name} ({counter})"
                    counter += 1

                destination = p.get('destination')
                schedule = p.get('schedule')
                c.execute(
                    'INSERT INTO profiles (name, paths, destination, schedule, options, retention, incremental) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (
                        name,
                        json.dumps(paths) if isinstance(paths, list) else paths,
                        json.dumps(destination) if destination else None,
                        json.dumps(schedule) if schedule else None,
                        p.get('options'),
                        p.get('retention', 0),
                        1 if p.get('incremental') else 0,
                    )
                )
                existing_names.add(name)
                imported += 1
            except Exception as e:
                errors.append(f'Profil "{p.get("name", "?")}": {str(e)}')
                skipped += 1

        conn.commit()
        conn.close()

        return jsonify({
            'success': True,
            'imported': imported,
            'skipped': skipped,
            'errors': errors,
            'message': f'Zaimportowano {imported} profili' + (f', pominięto {skipped}' if skipped else '')
        })
    except json.JSONDecodeError:
        return jsonify({'error': 'Nieprawidłowy format JSON'}), 400
    except Exception as e:
        return jsonify({'error': f'Błąd importu: {str(e)}'}), 500


@backup_bp.route('/profiles/<profile_id>/schedule', methods=['PUT'])
def update_profile_schedule(profile_id):
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,))
    if not c.fetchone():
        conn.close()
        return jsonify({'error': 'Profil nie znaleziony'}), 404
    c.execute('UPDATE profiles SET schedule=? WHERE id=?', (json.dumps(data.get('schedule')) if data.get('schedule') else None, profile_id))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@backup_bp.route('/profiles/<profile_id>', methods=['PUT'])
def update_profile(profile_id):
    data = request.json
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Profil nie znaleziony'}), 404
    c.execute('UPDATE profiles SET name=?, paths=?, destination=?, schedule=?, options=?, retention=?, incremental=? WHERE id=?', (
        data.get('name', row['name']),
        json.dumps(data.get('paths', json.loads(row['paths']))),
        json.dumps(data.get('destination')) if data.get('destination') else row['destination'],
        json.dumps(data.get('schedule')) if data.get('schedule') else row['schedule'],
        data.get('options', row['options']),
        data.get('retention', row['retention'] or 0),
        1 if data.get('incremental') else 0,
        profile_id
    ))
    conn.commit()
    # Re-fetch updated profile
    updated = conn.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,)).fetchone()
    conn.close()
    if updated:
        return jsonify({'success': True, 'profile': {
            'id': str(updated['id']), 'name': updated['name'],
            'paths': json.loads(updated['paths']),
            'destination': json.loads(updated['destination']) if updated['destination'] else None,
            'schedule': json.loads(updated['schedule']) if updated['schedule'] else None,
            'retention': updated['retention'] or 0,
            'incremental': bool(updated['incremental'])
        }})
    return jsonify({'success': True})

@backup_bp.route('/profiles/<profile_id>', methods=['DELETE'])
def delete_profile(profile_id):
    conn = get_db_connection()
    conn.execute('DELETE FROM profiles WHERE id = ?', (profile_id,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

@backup_bp.route('/profiles/<profile_id>/run', methods=['POST'])
def run_profile(profile_id):
    global current_operation
    conn = get_db_connection()
    row = conn.execute('SELECT * FROM profiles WHERE id = ?', (profile_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Profil nie znaleziony'}), 404
    profile = {
        'id': str(row['id']), 'name': row['name'],
        'paths': json.loads(row['paths']),
        'destination': json.loads(row['destination']) if row['destination'] else None,
        'retention': row['retention'] or 0,
        'incremental': bool(row['incremental']) if row['incremental'] else False,
    }
    conn.close()
    with operation_lock:
        if current_operation is not None:
            return jsonify({'error': 'Inna operacja jest w toku'}), 400
        current_operation = 'backup'

    destination = profile.get('destination')
    if destination and destination.get('type') == 'ssh':
        ssh_id = destination.get('server_id')
        if ssh_id:
            configs = load_ssh_configs()
            ssh_cfg = next((c for c in configs if c.get('id') == ssh_id), None)
            if ssh_cfg:
                destination['config'] = ssh_cfg

    _socketio.start_background_task(run_backup, profile['paths'], destination, profile['name'], profile['retention'], profile['incremental'])
    return jsonify({'success': True, 'message': f'Backup "{profile["name"]}" rozpoczęty'})

@backup_bp.route('/scheduled-backups')
def get_scheduled_backups():
    profiles = load_profiles()
    state = load_schedule_state()
    scheduled = []
    for p in profiles:
        schedule = p.get('schedule')
        if isinstance(schedule, str):
            try:
                schedule = json.loads(schedule)
            except Exception:
                continue
        if schedule and schedule.get('type') != 'manual':
            last_run = state.get(str(p['id']), {}).get('last_run')
            scheduled.append({
                'profile_id': p['id'], 'profile_name': p['name'],
                'schedule': schedule, 'last_run': last_run,
                'next_run': _calc_next_run(schedule, last_run)
            })
    return jsonify({'scheduled': scheduled})


# ═══════════════════════════════════════════════════════════
#  SYSTEM SNAPSHOTS  — Punkty przywracania systemu
#  Backs up: EthOS config/data, Docker compose projects,
#  Docker volumes, container list, system configs.
# ═══════════════════════════════════════════════════════════

SNAPSHOTS_DIR = os.path.join(BACKUP_DIR, 'snapshots')
os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

_snapshot_state = {
    'status': 'idle',   # idle | creating | restoring | done | error
    'percent': 0,
    'message': '',
    'log': [],
    '_started': 0,      # timestamp when operation started (for stuck detection)
}


def _snap_update(status=None, percent=None, message=None, log=None):
    if status:
        _snapshot_state['status'] = status
    if percent is not None:
        _snapshot_state['percent'] = percent
    if message:
        _snapshot_state['message'] = message
    if log:
        _snapshot_state['log'].append(log)
        _emit('snapshot_log', {'message': log})
    _emit('snapshot_progress', {
        'status': _snapshot_state['status'],
        'percent': _snapshot_state['percent'],
        'message': _snapshot_state['message'],
    })


def _docker_cmd(args, timeout=120):
    """Run a docker command, return (stdout, returncode)."""
    try:
        r = subprocess.run(
            ['docker'] + args,
            capture_output=True, text=True, timeout=timeout
        )
        return r.stdout.strip(), r.returncode
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return '', 1


def _docker_ok():
    _, rc = _docker_cmd(['info'], timeout=5)
    return rc == 0


@backup_bp.route('/snapshots', methods=['GET'])
def list_snapshots():
    """List all system snapshots (local + received from other NAS devices)."""
    snapshots = []
    if os.path.isdir(SNAPSHOTS_DIR):
        for name in sorted(os.listdir(SNAPSHOTS_DIR), reverse=True):
            snap_dir = os.path.join(SNAPSHOTS_DIR, name)
            if not os.path.isdir(snap_dir):
                continue
            meta_file = os.path.join(snap_dir, 'meta.json')
            if os.path.isfile(meta_file):
                try:
                    with open(meta_file, 'r') as f:
                        meta = json.load(f)
                    # Calculate total size
                    total_size = 0
                    for root, dirs, files in os.walk(snap_dir):
                        for fn in files:
                            total_size += os.path.getsize(os.path.join(root, fn))
                    meta['size'] = total_size
                    meta['dir'] = name
                    meta['received'] = False
                    snapshots.append(meta)
                except Exception:
                    pass

    # Also include received snapshots from other NAS devices
    try:
        received = _find_received_snapshot_dirs()
        for r in received:
            r['received'] = True
            snapshots.append(r)
    except Exception:
        logger.exception("Error scanning received snapshots for main list")

    # Sort all by creation date, newest first
    snapshots.sort(key=lambda s: s.get('created', ''), reverse=True)
    return jsonify({'snapshots': snapshots})


@backup_bp.route('/snapshots/status', methods=['GET'])
def snapshot_status():
    """Current snapshot operation status."""
    return jsonify(_snapshot_state)


@backup_bp.route('/snapshots/reset', methods=['POST'])
def reset_snapshot_state():
    """Manually reset a stuck snapshot operation state."""
    old_status = _snapshot_state['status']
    _snapshot_state['status'] = 'idle'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = ''
    _snapshot_state['log'] = []
    _snapshot_state['_started'] = 0
    logger.info(f"Snapshot state manually reset from '{old_status}' to 'idle'")
    return jsonify({'ok': True, 'previous_status': old_status})


@backup_bp.route('/snapshots', methods=['POST'])
def create_snapshot():
    """Create a new system snapshot."""
    # Detect stuck state: if operation running for more than 30 minutes, auto-reset
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        elapsed = time.time() - _snapshot_state.get('_started', 0)
        if elapsed > 1800:  # 30 minutes
            logger.warning(f"Snapshot state '{_snapshot_state['status']}' stuck for {elapsed:.0f}s — auto-resetting")
            _snapshot_state['status'] = 'idle'
        else:
            return jsonify({'error': 'Operacja snapshot w toku'}), 409

    data = request.json or {}
    label = data.get('label', '').strip()
    include_docker = data.get('include_docker', True)
    include_volumes = data.get('include_volumes', True)
    include_ethos = data.get('include_ethos', True)
    include_system = data.get('include_system', True)
    dest_type = data.get('dest_type', 'local')  # 'local' or 'usb'
    dest_path = data.get('dest_path', '')

    _snapshot_state['status'] = 'creating'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Rozpoczynam...'
    _snapshot_state['log'] = []
    _snapshot_state['_started'] = time.time()

    if _socketio:
        _socketio.start_background_task(
            _create_snapshot_worker,
            label, include_docker, include_volumes, include_ethos, include_system,
            dest_type, dest_path
        )
    else:
        logger.error("SocketIO not initialized — cannot start snapshot worker")
        _snapshot_state['status'] = 'error'
        _snapshot_state['message'] = 'Błąd wewnętrzny: SocketIO niezainicjalizowane'
    return jsonify({'ok': True, 'message': 'Tworzenie snapshota rozpoczęte'})


@backup_bp.route('/snapshots/<snap_id>/browse', methods=['GET'])
def browse_snapshot(snap_id):
    """Browse the contents of a snapshot directory."""
    if not re.match(r'^snap_\d{8}_\d{6}$', snap_id):
        return jsonify({'error': 'Nieprawidłowy identyfikator snapshota'}), 400
    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot nie istnieje'}), 404
    tree = []
    for root, dirs, files in os.walk(snap_dir):
        rel = os.path.relpath(root, snap_dir)
        if rel == '.':
            rel = ''
        for d in sorted(dirs):
            tree.append({'name': os.path.join(rel, d) if rel else d, 'type': 'dir', 'size': 0})
        for f in sorted(files):
            fp = os.path.join(root, f)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                sz = 0
            tree.append({'name': os.path.join(rel, f) if rel else f, 'type': 'file', 'size': sz})
    return jsonify({'tree': tree, 'snap_id': snap_id})


@backup_bp.route('/snapshots/space', methods=['GET'])
def snapshot_space():
    """Get total space used by snapshots and disk free space."""
    total_size = 0
    count = 0
    if os.path.isdir(SNAPSHOTS_DIR):
        for name in os.listdir(SNAPSHOTS_DIR):
            d = os.path.join(SNAPSHOTS_DIR, name)
            if os.path.isdir(d):
                count += 1
                for root, dirs, files in os.walk(d):
                    for f in files:
                        try:
                            total_size += os.path.getsize(os.path.join(root, f))
                        except OSError:
                            pass
    try:
        st = os.statvfs(SNAPSHOTS_DIR)
        disk_free = st.f_bavail * st.f_frsize
        disk_total = st.f_blocks * st.f_frsize
    except Exception:
        disk_free = 0
        disk_total = 0
    return jsonify({
        'snapshot_bytes': total_size,
        'snapshot_count': count,
        'disk_free': disk_free,
        'disk_total': disk_total,
    })


@backup_bp.route('/snapshots/<snap_id>', methods=['DELETE'])
def delete_snapshot(snap_id):
    """Delete a snapshot."""
    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot nie istnieje'}), 404
    shutil.rmtree(snap_dir, ignore_errors=True)
    return jsonify({'ok': True})


@backup_bp.route('/snapshots/<snap_id>/restore', methods=['POST'])
def restore_snapshot(snap_id):
    """Restore system from a snapshot."""
    if _snapshot_state['status'] in ('creating', 'restoring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot nie istnieje'}), 404

    data = request.json or {}
    restore_docker = data.get('restore_docker', True)
    restore_volumes = data.get('restore_volumes', True)
    restore_ethos = data.get('restore_ethos', True)
    restore_system = data.get('restore_system', True)

    _snapshot_state['status'] = 'restoring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Rozpoczynam przywracanie...'
    _snapshot_state['log'] = []

    if _socketio:
        _socketio.start_background_task(
            _restore_snapshot_worker,
            snap_dir, restore_docker, restore_volumes, restore_ethos, restore_system
        )
    return jsonify({'ok': True, 'message': 'Przywracanie rozpoczęte'})


@backup_bp.route('/snapshots/<snap_id>/download')
def download_snapshot(snap_id):
    """Download snapshot as tar.gz."""
    from flask import send_file as _send
    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot nie istnieje'}), 404
    archive = os.path.join(SNAPSHOTS_DIR, f'{snap_id}.tar.gz')
    if not os.path.isfile(archive):
        subprocess.run(['tar', '-czf', archive, '-C', SNAPSHOTS_DIR, snap_id],
                       capture_output=True, timeout=600)
    if os.path.isfile(archive):
        return _send(archive, as_attachment=True, download_name=f'snapshot-{snap_id}.tar.gz')
    return jsonify({'error': 'Nie udało się spakować'}), 500


# ── NAS-to-NAS snapshot transfer ──

@backup_bp.route('/snapshots/<snap_id>/transfer', methods=['POST'])
def transfer_snapshot(snap_id):
    """Transfer (push) a snapshot to a remote NAS via SSH/SCP."""
    if not HAS_SSH:
        return jsonify({'error': 'paramiko/scp nie zainstalowane'}), 500
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    snap_dir = os.path.join(SNAPSHOTS_DIR, snap_id)
    if not os.path.isdir(snap_dir):
        return jsonify({'error': 'Snapshot nie istnieje'}), 404

    data = request.json or {}
    server_id = data.get('server_id')
    if not server_id:
        return jsonify({'error': 'Nie wybrano serwera'}), 400

    configs = load_ssh_configs()
    srv = next((c for c in configs if c.get('id') == server_id), None)
    if not srv:
        return jsonify({'error': 'Serwer SSH nie znaleziony'}), 404

    _snapshot_state['status'] = 'transferring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Rozpoczynam transfer...'
    _snapshot_state['log'] = []

    if _socketio:
        _socketio.start_background_task(
            _transfer_snapshot_worker, snap_id, snap_dir, srv
        )
    return jsonify({'ok': True, 'message': 'Transfer rozpoczęty'})


def _ssh_resolve_remote_path(ssh, raw_path):
    """Resolve ~, $HOME, etc. in remote path and return absolute path."""
    try:
        # Let the remote shell expand ~ and $HOME
        _, so, _ = ssh.exec_command(f'eval echo {raw_path}')
        resolved = so.read().decode().strip()
        if resolved and resolved != '' and not resolved.startswith('$'):
            return resolved
    except Exception:
        pass
    # Fallback: resolve $HOME manually
    home = _ssh_resolve_home(ssh)
    if home:
        return raw_path.replace('~', home).replace('$HOME', home)
    return raw_path


def _transfer_snapshot_worker(snap_id, snap_dir, ssh_config):
    """Background: tar snapshot, SCP to remote, optionally extract."""
    try:
        _snap_update(percent=5, message='Pakowanie snapshota...', log=f'Archiwizuję {snap_id}...')

        archive = os.path.join(SNAPSHOTS_DIR, f'{snap_id}.tar.gz')
        if not os.path.isfile(archive):
            r = subprocess.run(
                ['tar', '-czf', archive, '-C', SNAPSHOTS_DIR, snap_id],
                capture_output=True, text=True, timeout=600
            )
            if r.returncode != 0:
                _snap_update(status='error', message=f'Błąd archiwizacji: {r.stderr[:200]}')
                return

        arc_size = os.path.getsize(archive)
        _snap_update(percent=15, message='Łączenie z serwerem...',
                     log=f'Archiwum: {arc_size / 1048576:.1f} MB')

        ssh = _get_ssh_client(ssh_config['host'], ssh_config.get('port', 22),
                              ssh_config['username'],
                              password=ssh_config.get('password'),
                              key_path=ssh_config.get('key_path'), timeout=30)
        _snap_update(percent=20, message='Połączono, przygotowuję...', log=f'SSH → {ssh_config["host"]}')

        # Resolve the remote path (handle ~, $HOME, relative paths)
        raw_remote_path = ssh_config.get('remote_path', '~/backups')
        remote_path = _ssh_resolve_remote_path(ssh, raw_remote_path)
        remote_snap_dir = f'{remote_path}/snapshots'

        # Create remote directory and verify it's writable
        writable, _ = _ssh_ensure_writable_dir(ssh, remote_snap_dir)

        if not writable:
            _snap_update(log=f'Brak uprawnień do {remote_snap_dir} — szukam alternatywnej ścieżki...')
            # Try $HOME/backups/snapshots as fallback
            remote_home = _ssh_resolve_home(ssh)
            fallback_paths = []
            if remote_home:
                fallback_paths.append(f'{remote_home}/backups/snapshots')
            fallback_paths.append('/tmp/ethos-snapshots')

            found_writable = False
            for fb_path in fallback_paths:
                fb_ok, _ = _ssh_ensure_writable_dir(ssh, fb_path)
                if fb_ok:
                    remote_snap_dir = fb_path
                    _snap_update(log=f'Używam ścieżki: {remote_snap_dir}')
                    found_writable = True
                    break

            if not found_writable:
                _snap_update(status='error',
                             message=f'Brak uprawnień zapisu na zdalnym serwerze. Sprawdź ścieżkę: {raw_remote_path}',
                             log=f'BŁĄD: Nie można zapisać do {remote_snap_dir} ani do ścieżek fallback')
                ssh.close()
                return

        _snap_update(percent=22, message='Przesyłam...', log=f'Docelowy katalog: {remote_snap_dir}')

        remote_file = f'{remote_snap_dir}/{snap_id}.tar.gz'

        def scp_progress(filename, size, sent):
            pct = int(22 + (sent / max(size, 1)) * 58)
            _snap_update(
                percent=pct,
                message=f'Przesyłanie... {sent / 1048576:.0f}/{size / 1048576:.0f} MB'
            )

        with SCPClient(ssh.get_transport(), progress=scp_progress) as scp:
            scp.put(archive, remote_file)

        _snap_update(percent=85, message='Rozpakowywanie na zdalnym serwerze...',
                     log='Transfer zakończony, rozpakowuję...')

        # Extract on remote
        cmd = f'cd {remote_snap_dir} && tar -xzf {snap_id}.tar.gz && rm -f {snap_id}.tar.gz'
        stdin_, stdout_, stderr_ = ssh.exec_command(cmd, timeout=300)
        exit_code = stdout_.channel.recv_exit_status()
        if exit_code == 0:
            _snap_update(log='Rozpakowano na zdalnym serwerze ✓')
        else:
            err_txt = stderr_.read().decode()[:200]
            _snap_update(log=f'Rozpakowanie: kod {exit_code} — {err_txt}')

        ssh.close()

        _snap_update(status='done', percent=100,
                     message=f'Snapshot przesłany do {ssh_config["host"]}:{remote_snap_dir}')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Błąd transferu: {e}',
                     log=f'WYJĄTEK: {e}')
        logger.exception("Snapshot transfer failed")


@backup_bp.route('/snapshots/import', methods=['POST'])
def import_snapshot():
    """Import snapshot from uploaded tar.gz file."""
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'Brak pliku'}), 400

    if not f.filename.endswith('.tar.gz'):
        return jsonify({'error': 'Wymagany plik .tar.gz'}), 400

    os.makedirs(SNAPSHOTS_DIR, exist_ok=True)

    # Save uploaded file to temp location
    tmp_archive = os.path.join(SNAPSHOTS_DIR, f'_import_{secrets.token_hex(4)}.tar.gz')
    try:
        f.save(tmp_archive)
        _snap_update(status='creating', percent=30,
                     message='Rozpakowywanie importu...', log=['Plik otrzymany, rozpakowuję...'])

        # Extract
        r = subprocess.run(
            ['tar', '-xzf', tmp_archive, '-C', SNAPSHOTS_DIR],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode != 0:
            _snap_update(status='error', message=f'Błąd rozpakowywania: {r.stderr[:200]}')
            return jsonify({'error': f'Błąd rozpakowywania: {r.stderr[:200]}'}), 500

        # Find extracted snapshot (directory with meta.json)
        imported_name = None
        for entry in os.listdir(SNAPSHOTS_DIR):
            entry_path = os.path.join(SNAPSHOTS_DIR, entry)
            if os.path.isdir(entry_path) and os.path.isfile(os.path.join(entry_path, 'meta.json')):
                # Check if this is new (not in our known list before import)
                meta_file = os.path.join(entry_path, 'meta.json')
                try:
                    with open(meta_file, 'r') as mf:
                        m = json.load(mf)
                    imported_name = entry
                except Exception:
                    pass

        if not imported_name:
            _snap_update(status='error', message='Nie znaleziono snapshota w archiwum')
            return jsonify({'error': 'Nie znaleziono snapshota w archiwum'}), 400

        _snap_update(status='done', percent=100,
                     message=f'Snapshot "{imported_name}" zaimportowany!')
        return jsonify({'ok': True, 'snapshot': imported_name})

    except Exception as e:
        _snap_update(status='error', message=f'Błąd importu: {e}')
        return jsonify({'error': str(e)}), 500
    finally:
        # Cleanup temp archive
        if os.path.isfile(tmp_archive):
            os.remove(tmp_archive)


# ── Received snapshots (from other NAS devices) ──

def _find_received_snapshot_dirs():
    """
    Scan common directories where snapshots from other NAS devices
    might have been received via SSH transfer.
    Returns list of (base_dir, snap_name, meta) for directories
    that contain valid snapshots and are NOT in the local SNAPSHOTS_DIR.
    """
    local_real = os.path.realpath(SNAPSHOTS_DIR)
    scan_paths = set()

    # 1) User home + /backups/snapshots (default ~/backups target)
    home = os.path.expanduser('~')
    scan_paths.add(os.path.join(home, 'backups', 'snapshots'))

    # 2) /backups/snapshots (if someone used /backups as remote_path)
    scan_paths.add('/backups/snapshots')

    # 3) /tmp/ethos-snapshots (fallback path used by transfer code)
    scan_paths.add('/tmp/ethos-snapshots')

    # 4) Check all home directories for /backups/snapshots
    try:
        for entry in os.scandir('/home'):
            if entry.is_dir():
                p = os.path.join(entry.path, 'backups', 'snapshots')
                scan_paths.add(p)
    except Exception:
        pass

    # 5) Check EthOS data paths (common on EthOS installs)
    for base in ['/data', '/media']:
        try:
            for root, dirs, files in os.walk(base, topdown=True):
                # Look max 4 levels deep
                depth = root.replace(base, '').count(os.sep)
                if depth > 4:
                    dirs.clear()
                    continue
                if os.path.basename(root) == 'snapshots':
                    parent = os.path.dirname(root)
                    if os.path.basename(parent) == 'backups':
                        scan_paths.add(root)
                    dirs.clear()
        except Exception:
            pass

    results = []
    seen_snap_ids = set()

    for scan_dir in scan_paths:
        if not os.path.isdir(scan_dir):
            continue
        # Skip if this IS the local SNAPSHOTS_DIR
        if os.path.realpath(scan_dir) == local_real:
            continue

        try:
            for name in os.listdir(scan_dir):
                snap_path = os.path.join(scan_dir, name)
                if not os.path.isdir(snap_path):
                    continue
                meta_file = os.path.join(snap_path, 'meta.json')
                if not os.path.isfile(meta_file):
                    continue
                # Skip if same name already exists in local SNAPSHOTS_DIR
                if os.path.isdir(os.path.join(SNAPSHOTS_DIR, name)):
                    continue
                # Avoid duplicates across scan paths
                if name in seen_snap_ids:
                    continue
                seen_snap_ids.add(name)
                try:
                    with open(meta_file, 'r') as f:
                        meta = json.load(f)
                    # Calculate size
                    total_size = 0
                    for root, dirs, files in os.walk(snap_path):
                        for fn in files:
                            try:
                                total_size += os.path.getsize(os.path.join(root, fn))
                            except OSError:
                                pass
                    meta['size'] = total_size
                    meta['dir'] = name
                    meta['source_path'] = snap_path
                    meta['received'] = True
                    results.append(meta)
                except Exception:
                    pass
        except PermissionError:
            pass

    results.sort(key=lambda s: s.get('created', ''), reverse=True)
    return results


@backup_bp.route('/snapshots/received', methods=['GET'])
def list_received_snapshots():
    """List snapshots received from other NAS devices (not in local SNAPSHOTS_DIR)."""
    try:
        snaps = _find_received_snapshot_dirs()
        return jsonify({'snapshots': snaps})
    except Exception as e:
        logger.exception("Error scanning received snapshots")
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/snapshots/received/adopt', methods=['POST'])
def adopt_received_snapshot():
    """Copy/move a received snapshot into local SNAPSHOTS_DIR so it appears in the normal list."""
    data = request.json or {}
    source_path = data.get('source_path')
    snap_dir_name = data.get('snap_id')
    if not source_path or not snap_dir_name:
        return jsonify({'error': 'source_path i snap_id wymagane'}), 400

    # Validate the source path contains a valid snapshot
    meta_file = os.path.join(source_path, 'meta.json')
    if not os.path.isfile(meta_file):
        return jsonify({'error': 'Nieprawidłowa ścieżka snapshota'}), 404

    local_dest = os.path.join(SNAPSHOTS_DIR, snap_dir_name)
    if os.path.exists(local_dest):
        return jsonify({'error': 'Snapshot o tej nazwie już istnieje lokalnie'}), 409

    try:
        # Copy the snapshot directory into local SNAPSHOTS_DIR
        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        shutil.copytree(source_path, local_dest)
        return jsonify({'ok': True, 'message': f'Snapshot {snap_dir_name} zaimportowany do lokalnego katalogu'})
    except Exception as e:
        logger.exception("Error adopting received snapshot")
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/snapshots/received/restore', methods=['POST'])
def restore_received_snapshot():
    """Restore from a received snapshot (in-place, without copying to SNAPSHOTS_DIR first)."""
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    data = request.json or {}
    source_path = data.get('source_path')
    if not source_path:
        return jsonify({'error': 'source_path wymagane'}), 400

    meta_file = os.path.join(source_path, 'meta.json')
    if not os.path.isfile(meta_file):
        return jsonify({'error': 'Nieprawidłowa ścieżka snapshota'}), 404

    restore_ethos = data.get('restore_ethos', True)
    restore_system = data.get('restore_system', True)
    restore_docker = data.get('restore_docker', True)
    restore_volumes = data.get('restore_volumes', True)

    _snapshot_state['status'] = 'restoring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Przywracanie z otrzymanego snapshota...'
    _snapshot_state['log'] = []
    _snapshot_state['_started'] = time.time()

    if _socketio:
        _socketio.start_background_task(
            _restore_snapshot_worker, source_path,
            restore_docker, restore_volumes, restore_ethos, restore_system
        )
    return jsonify({'ok': True, 'message': 'Przywracanie rozpoczęte'})


@backup_bp.route('/snapshots/remote', methods=['POST'])
def list_remote_snapshots():
    """List snapshots on a remote NAS (via SSH)."""
    if not HAS_SSH:
        return jsonify({'error': 'paramiko nie zainstalowane'}), 500

    data = request.json or {}
    server_id = data.get('server_id')
    if not server_id:
        return jsonify({'error': 'Nie wybrano serwera'}), 400

    configs = load_ssh_configs()
    srv = next((c for c in configs if c.get('id') == server_id), None)
    if not srv:
        return jsonify({'error': 'Serwer SSH nie znaleziony'}), 404

    try:
        ssh = _get_ssh_client(srv['host'], srv.get('port', 22),
                              srv['username'],
                              password=srv.get('password'),
                              key_path=srv.get('key_path'), timeout=15)

        remote_path = srv.get('remote_path', '~/backups')
        # Resolve ~ and $HOME on the remote
        remote_path = _ssh_resolve_remote_path(ssh, remote_path)
        remote_snap_dir = f'{remote_path}/snapshots'

        # Read meta.json from each snapshot directory
        cmd = f'for d in {remote_snap_dir}/snap_*/; do [ -f "$d/meta.json" ] && echo "---" && cat "$d/meta.json" && echo "|||$(basename $d)|||$(du -sb $d | cut -f1)"; done 2>/dev/null'
        stdin_, stdout_, stderr_ = ssh.exec_command(cmd, timeout=30)
        stdout_.channel.recv_exit_status()
        output = stdout_.read().decode()
        ssh.close()

        snapshots = []
        for block in output.split('---'):
            block = block.strip()
            if not block:
                continue
            # Parse: JSON content then |||dirname|||size
            parts = block.rsplit('|||', 2)
            if len(parts) >= 3:
                json_str = parts[0].strip()
                snap_dirname = parts[1]
                snap_size = int(parts[2]) if parts[2].isdigit() else 0
                try:
                    meta = json.loads(json_str)
                    meta['dir'] = snap_dirname
                    meta['size'] = snap_size
                    meta['remote'] = True
                    snapshots.append(meta)
                except Exception:
                    pass

        snapshots.sort(key=lambda s: s.get('created', ''), reverse=True)
        return jsonify({'snapshots': snapshots, 'host': srv['host']})

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@backup_bp.route('/snapshots/pull', methods=['POST'])
def pull_remote_snapshot():
    """Pull (download) a snapshot from remote NAS to local."""
    if not HAS_SSH:
        return jsonify({'error': 'paramiko/scp nie zainstalowane'}), 500
    if _snapshot_state['status'] in ('creating', 'restoring', 'transferring'):
        return jsonify({'error': 'Operacja snapshot w toku'}), 409

    data = request.json or {}
    server_id = data.get('server_id')
    remote_snap_id = data.get('snap_id')
    if not server_id or not remote_snap_id:
        return jsonify({'error': 'server_id i snap_id wymagane'}), 400

    configs = load_ssh_configs()
    srv = next((c for c in configs if c.get('id') == server_id), None)
    if not srv:
        return jsonify({'error': 'Serwer SSH nie znaleziony'}), 404

    _snapshot_state['status'] = 'transferring'
    _snapshot_state['percent'] = 0
    _snapshot_state['message'] = 'Pobieranie ze zdalnego NAS...'
    _snapshot_state['log'] = []

    if _socketio:
        _socketio.start_background_task(
            _pull_snapshot_worker, remote_snap_id, srv
        )
    return jsonify({'ok': True, 'message': 'Pobieranie rozpoczęte'})


def _pull_snapshot_worker(snap_id, ssh_config):
    """Background: SCP snapshot from remote NAS to local."""
    tmp_archive = None
    try:
        _snap_update(percent=5, message='Łączenie z serwerem...', log=f'Łączę z {ssh_config["host"]}...')

        ssh = _get_ssh_client(ssh_config['host'], ssh_config.get('port', 22),
                              ssh_config['username'],
                              password=ssh_config.get('password'),
                              key_path=ssh_config.get('key_path'), timeout=30)

        remote_path = ssh_config.get('remote_path', '~/backups')
        # Resolve ~ and $HOME on the remote
        remote_path = _ssh_resolve_remote_path(ssh, remote_path)
        remote_snap_dir = f'{remote_path}/snapshots'
        remote_archive = f'/tmp/_ethos_pull_{snap_id}.tar.gz'

        # Archive snapshot on remote
        _snap_update(percent=10, message='Pakowanie na zdalnym serwerze...',
                     log='Archiwizacja na zdalnym...')
        cmd = f'tar -czf {remote_archive} -C {remote_snap_dir} {snap_id}'
        stdin_, stdout_, stderr_ = ssh.exec_command(cmd, timeout=600)
        exit_code = stdout_.channel.recv_exit_status()
        if exit_code != 0:
            err = stderr_.read().decode()[:200]
            _snap_update(status='error', message=f'Błąd pakowania: {err}')
            ssh.close()
            return

        _snap_update(percent=20, message='Pobieranie...',
                     log='Transfer ze zdalnego serwera...')

        os.makedirs(SNAPSHOTS_DIR, exist_ok=True)
        tmp_archive = os.path.join(SNAPSHOTS_DIR, f'_pull_{snap_id}.tar.gz')

        def scp_progress(filename, size, sent):
            pct = int(20 + (sent / max(size, 1)) * 60)
            _snap_update(
                percent=pct,
                message=f'Pobieranie... {sent / 1048576:.0f}/{size / 1048576:.0f} MB'
            )

        with SCPClient(ssh.get_transport(), progress=scp_progress) as scp:
            scp.get(remote_archive, tmp_archive)

        # Clean up remote archive
        ssh.exec_command(f'rm -f {remote_archive}')
        ssh.close()

        _snap_update(percent=85, message='Rozpakowywanie...',
                     log='Rozpakowuję snapshot...')

        r = subprocess.run(
            ['tar', '-xzf', tmp_archive, '-C', SNAPSHOTS_DIR],
            capture_output=True, text=True, timeout=600
        )
        if r.returncode != 0:
            _snap_update(status='error', message=f'Błąd rozpakowywania: {r.stderr[:200]}')
            return

        _snap_update(status='done', percent=100,
                     message=f'Snapshot {snap_id} pobrany ze zdalnego NAS!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Błąd: {e}',
                     log=f'WYJĄTEK: {e}')
        logger.exception("Snapshot pull failed")
    finally:
        if tmp_archive and os.path.isfile(tmp_archive):
            os.remove(tmp_archive)


# ── NAS discovery on LAN ──

def _get_local_ip():
    """Best-effort: get this machine's LAN IP."""
    import socket as _sock
    try:
        s = _sock.socket(_sock.AF_INET, _sock.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


def _discover_via_avahi():
    """Use avahi-browse to find _ethos._tcp services on LAN."""
    devices = []
    try:
        r = subprocess.run(
            ['avahi-browse', '-ptr', '_ethos._tcp', 'local', '--resolve', '-l'],
            capture_output=True, text=True, timeout=8
        )
        # Parse avahi-browse output:
        # =;eth0;IPv4;NAS Name;_ethos._tcp;local;hostname.local;192.168.50.100;9000;"name=X" "version=Y"
        for line in r.stdout.splitlines():
            if not line.startswith('='):
                continue
            parts = line.split(';')
            if len(parts) < 9:
                continue
            name = parts[3]
            ip = parts[7]
            port = int(parts[8]) if parts[8].isdigit() else 9000
            # Parse TXT records
            txt = {}
            for tp in parts[9:]:
                tp = tp.strip().strip('"')
                if '=' in tp:
                    k, v = tp.split('=', 1)
                    txt[k] = v
            devices.append({
                'name': txt.get('name', name),
                'hostname': parts[6].rstrip('.'),
                'ip': ip,
                'port': port,
                'version': txt.get('version', '?'),
                'source': 'mdns',
            })
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        pass
    return devices


def _discover_via_scan(port=9000, timeout=2.0):
    """Scan local /24 subnet on given port for EthOS identify endpoint."""
    import socket as _sock
    from concurrent.futures import ThreadPoolExecutor, as_completed

    my_ip = _get_local_ip()
    if my_ip == '127.0.0.1':
        return []

    prefix = '.'.join(my_ip.split('.')[:3])
    devices = []

    def _probe(ip):
        try:
            s = _sock.socket(_sock.AF_INET, _sock.SOCK_STREAM)
            s.settimeout(timeout)
            result = s.connect_ex((ip, port))
            s.close()
            if result != 0:
                return None
            # Port open — try identify endpoint
            import urllib.request
            url = f'http://{ip}:{port}/api/ethos/identify'
            req = urllib.request.Request(url, headers={'Accept': 'application/json'})
            resp = urllib.request.urlopen(req, timeout=timeout)
            data = json.loads(resp.read().decode())
            if data.get('ethos'):
                return {
                    'name': data.get('name', '?'),
                    'hostname': data.get('hostname', '?'),
                    'ip': ip,
                    'port': data.get('port', port),
                    'version': data.get('version', '?'),
                    'source': 'scan',
                }
        except Exception:
            pass
        return None

    with ThreadPoolExecutor(max_workers=50) as pool:
        futures = {pool.submit(_probe, f'{prefix}.{i}'): i for i in range(1, 255)}
        try:
            for fut in as_completed(futures, timeout=timeout + 5):
                try:
                    res = fut.result(timeout=0.5)
                    if res:
                        devices.append(res)
                except Exception:
                    pass
        except (TimeoutError, FuturesTimeoutError):
            # Some hosts didn't respond in time — that's OK
            pass

    return devices


@backup_bp.route('/discover-nas', methods=['POST'])
def discover_nas():
    """Discover other EthOS instances on the local network."""
    my_ip = _get_local_ip()

    # Method 1: Avahi mDNS
    devices = _discover_via_avahi()

    # Method 2: Subnet scan fallback / supplement
    scanned = _discover_via_scan()

    # Merge: deduplicate by IP
    known_ips = {d['ip'] for d in devices}
    for sd in scanned:
        if sd['ip'] not in known_ips:
            devices.append(sd)
            known_ips.add(sd['ip'])

    # Filter out self
    devices = [d for d in devices if d['ip'] != my_ip]

    return jsonify({'devices': devices, 'my_ip': my_ip})


# ── Snapshot creation worker ──

def _create_snapshot_worker(label, include_docker, include_volumes,
                             include_ethos, include_system, dest_type, dest_path):
    """Background task: creates a full system snapshot."""
    try:
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        snap_name = f'snap_{ts}'
        snap_dir = os.path.join(SNAPSHOTS_DIR, snap_name)
        os.makedirs(snap_dir, exist_ok=True)

        meta = {
            'id': snap_name,
            'label': label or f'Snapshot {datetime.now().strftime("%Y-%m-%d %H:%M")}',
            'created': datetime.now().isoformat(),
            'hostname': _read_file_safe('/etc/hostname', 'unknown').strip(),
            'includes': {
                'ethos': include_ethos,
                'docker': include_docker,
                'volumes': include_volumes,
                'system': include_system,
            },
            'docker_projects': [],
            'docker_volumes': [],
            'docker_containers': 0,
        }

        steps_total = sum([include_ethos, include_system, include_docker, include_volumes])
        step_i = 0

        # ── 1. EthOS config & data ──
        if include_ethos:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backupuję EthOS...', log='Kopiowanie konfiguracji EthOS...')

            ethos_root = os.environ.get('ETHOS_ROOT', '/home/marcin/docker/nasos')
            ethos_dir = os.path.join(snap_dir, 'ethos')
            os.makedirs(ethos_dir, exist_ok=True)

            # ethos.env
            env_file = os.path.join(ethos_root, 'ethos.env')
            if os.path.isfile(env_file):
                shutil.copy2(env_file, os.path.join(ethos_dir, 'ethos.env'))

            # data/ (settings, configs, profiles DB, etc.)
            data_src = os.path.join(ethos_root, 'data')
            if os.path.isdir(data_src):
                subprocess.run(
                    ['tar', '-czf', os.path.join(ethos_dir, 'data.tar.gz'),
                     '-C', ethos_root, 'data'],
                    capture_output=True, timeout=120
                )
                _snap_update(log=f'EthOS data/ ({_dir_size_str(data_src)})')

            # install.conf
            for extra in ['install.conf']:
                src = os.path.join(ethos_root, extra)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(ethos_dir, extra))

            _snap_update(log='EthOS — gotowe')

        # ── 2. System configs ──
        if include_system:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backupuję konfigurację systemu...', log='Kopiowanie konfiguracji systemu...')

            sys_dir = os.path.join(snap_dir, 'system')
            os.makedirs(sys_dir, exist_ok=True)

            # Hostname, hosts, timezone, locale
            for f in ['/etc/hostname', '/etc/hosts', '/etc/timezone',
                       '/etc/default/locale', '/etc/resolv.conf']:
                if os.path.isfile(f):
                    dest = os.path.join(sys_dir, os.path.basename(f))
                    try:
                        shutil.copy2(f, dest)
                    except Exception:
                        pass

            # Timezone (symlink)
            try:
                tz = os.path.realpath('/etc/localtime')
                with open(os.path.join(sys_dir, 'timezone_link'), 'w') as tf:
                    tf.write(tz)
            except Exception:
                pass

            # NetworkManager connections
            nm_dir = '/etc/NetworkManager/system-connections'
            if os.path.isdir(nm_dir):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'nm-connections.tar.gz'),
                     '-C', '/etc/NetworkManager', 'system-connections'],
                    capture_output=True, timeout=30
                )
                _snap_update(log='NetworkManager connections saved')

            # Nginx sites
            nginx_avail = '/etc/nginx/sites-available'
            if os.path.isdir(nginx_avail):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'nginx-sites.tar.gz'),
                     '-C', '/etc/nginx', 'sites-available', 'sites-enabled'],
                    capture_output=True, timeout=30
                )
                _snap_update(log='Nginx sites saved')

            # SSL certs (Let's Encrypt)
            le_dir = '/etc/letsencrypt'
            if os.path.isdir(le_dir):
                subprocess.run(
                    ['tar', '-czf', os.path.join(sys_dir, 'letsencrypt.tar.gz'),
                     '-C', '/etc', 'letsencrypt'],
                    capture_output=True, timeout=60
                )
                _snap_update(log='Let\'s Encrypt certs saved')

            # Samba config
            smb_conf = '/etc/samba/smb.conf'
            if os.path.isfile(smb_conf):
                shutil.copy2(smb_conf, os.path.join(sys_dir, 'smb.conf'))
                _snap_update(log='Samba config saved')

            # fstab
            if os.path.isfile('/etc/fstab'):
                shutil.copy2('/etc/fstab', os.path.join(sys_dir, 'fstab'))

            # Crontab
            try:
                r = subprocess.run(['crontab', '-l'], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    with open(os.path.join(sys_dir, 'crontab'), 'w') as cf:
                        cf.write(r.stdout)
            except Exception:
                pass

            _snap_update(log='System config — gotowe')

        # ── 3. Docker compose projects ──
        if include_docker:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backupuję Docker...', log='Zapisywanie projektów Docker...')

            docker_dir = os.path.join(snap_dir, 'docker')
            os.makedirs(docker_dir, exist_ok=True)

            if _docker_ok():
                # Compose projects — save compose files + .env
                compose_root = os.environ.get('COMPOSE_ROOT', '/home/marcin/docker')
                projects = []
                if os.path.isdir(compose_root):
                    for entry in sorted(os.listdir(compose_root)):
                        proj_path = os.path.join(compose_root, entry)
                        if not os.path.isdir(proj_path):
                            continue
                        compose_file = None
                        for cf_name in ['docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml']:
                            candidate = os.path.join(proj_path, cf_name)
                            if os.path.isfile(candidate):
                                compose_file = candidate
                                break
                        if not compose_file:
                            continue

                        proj_bak = os.path.join(docker_dir, 'projects', entry)
                        os.makedirs(proj_bak, exist_ok=True)
                        shutil.copy2(compose_file, os.path.join(proj_bak, os.path.basename(compose_file)))
                        # .env file
                        env_f = os.path.join(proj_path, '.env')
                        if os.path.isfile(env_f):
                            shutil.copy2(env_f, os.path.join(proj_bak, '.env'))
                        # Additional env files referenced in compose
                        for extra_env in _find_env_files(compose_file):
                            extra_path = os.path.join(proj_path, extra_env)
                            if os.path.isfile(extra_path):
                                shutil.copy2(extra_path, os.path.join(proj_bak, extra_env))

                        projects.append(entry)
                        _snap_update(log=f'  Projekt: {entry}')

                meta['docker_projects'] = projects

                # Container list (for reference — what was running)
                out, rc = _docker_cmd(['ps', '-a', '--format', '{{json .}}', '--no-trunc'])
                if rc == 0:
                    containers = []
                    for line in out.split('\n'):
                        line = line.strip()
                        if line:
                            try:
                                containers.append(json.loads(line))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'containers.json'), 'w') as cf:
                        json.dump(containers, cf, indent=2)
                    meta['docker_containers'] = len(containers)
                    _snap_update(log=f'Kontenerów: {len(containers)}')

                # Docker images list
                out, rc = _docker_cmd(['images', '--format', '{{json .}}'])
                if rc == 0:
                    images = []
                    for line in out.split('\n'):
                        line = line.strip()
                        if line:
                            try:
                                images.append(json.loads(line))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'images.json'), 'w') as cf:
                        json.dump(images, cf, indent=2)
                    _snap_update(log=f'Obrazów Docker: {len(images)}')

                # Networks
                out, rc = _docker_cmd(['network', 'ls', '--format', '{{json .}}'])
                if rc == 0:
                    networks = []
                    for line in out.split('\n'):
                        if line.strip():
                            try:
                                networks.append(json.loads(line.strip()))
                            except Exception:
                                pass
                    with open(os.path.join(docker_dir, 'networks.json'), 'w') as cf:
                        json.dump(networks, cf, indent=2)

            else:
                _snap_update(log='Docker niedostępny — pomijam')

            _snap_update(log='Docker projects — gotowe')

        # ── 4. Docker volumes ──
        if include_volumes and include_docker:
            step_i += 1
            pct = int(step_i / steps_total * 90)
            _snap_update(percent=pct, message='Backupuję Docker volumes...', log='Eksportowanie wolumenów Docker...')

            vol_dir = os.path.join(snap_dir, 'docker', 'volumes')
            os.makedirs(vol_dir, exist_ok=True)

            if _docker_ok():
                out, rc = _docker_cmd(['volume', 'ls', '-q'])
                vol_names = [v.strip() for v in out.split('\n') if v.strip()] if rc == 0 else []
                saved_vols = []

                for vi, vol in enumerate(vol_names):
                    # Skip very large or internal volumes
                    inspect_out, irc = _docker_cmd(['volume', 'inspect', vol])
                    if irc != 0:
                        continue
                    try:
                        vinfo = json.loads(inspect_out)
                        if isinstance(vinfo, list) and vinfo:
                            vinfo = vinfo[0]
                        mount = vinfo.get('Mountpoint', '')
                    except Exception:
                        mount = ''

                    # Check size — skip if > 5GB
                    vol_size = 0
                    if mount and os.path.isdir(mount):
                        try:
                            r = subprocess.run(
                                ['du', '-sb', mount],
                                capture_output=True, text=True, timeout=10
                            )
                            if r.returncode == 0:
                                vol_size = int(r.stdout.split()[0])
                        except Exception:
                            pass

                    size_mb = vol_size / (1024 * 1024)
                    if vol_size > 5 * 1024 * 1024 * 1024:  # >5GB
                        _snap_update(log=f'  Pomijam {vol} ({size_mb:.0f} MB — za duży)')
                        continue

                    vol_pct = int(pct + (vi / max(len(vol_names), 1)) * (90 - pct))
                    _snap_update(
                        percent=vol_pct,
                        message=f'Eksport: {vol} ({size_mb:.1f} MB)',
                        log=f'  Volume: {vol} ({size_mb:.1f} MB)'
                    )

                    archive = os.path.join(vol_dir, f'{vol}.tar.gz')
                    # Export volume via docker run
                    result = subprocess.run(
                        ['docker', 'run', '--rm',
                         '-v', f'{vol}:/volume_data:ro',
                         '-v', f'{vol_dir}:/backup',
                         'alpine',
                         'tar', '-czf', f'/backup/{vol}.tar.gz', '-C', '/volume_data', '.'],
                        capture_output=True, text=True, timeout=600
                    )
                    if result.returncode == 0 and os.path.isfile(archive):
                        saved_vols.append(vol)
                    else:
                        _snap_update(log=f'  BŁĄD eksportu {vol}: {result.stderr[:200]}')

                meta['docker_volumes'] = saved_vols
                _snap_update(log=f'Volumes: {len(saved_vols)}/{len(vol_names)} wyeksportowane')
            else:
                _snap_update(log='Docker niedostępny — pomijam volumes')

        # ── Save metadata ──
        _snap_update(percent=92, message='Zapisywanie metadanych...')
        with open(os.path.join(snap_dir, 'meta.json'), 'w') as mf:
            json.dump(meta, mf, indent=2)

        # ── Copy to USB if requested ──
        if dest_type == 'usb' and dest_path:
            _snap_update(percent=93, message='Kopiowanie na USB...', log=f'Transfer na USB: {dest_path}')
            usb_snap_dir = os.path.join(dest_path, 'ethos-snapshots')
            os.makedirs(usb_snap_dir, exist_ok=True)
            archive_name = f'{snap_name}.tar.gz'
            archive_path = os.path.join(usb_snap_dir, archive_name)
            r = subprocess.run(
                ['tar', '-czf', archive_path, '-C', SNAPSHOTS_DIR, snap_name],
                capture_output=True, timeout=600
            )
            if r.returncode == 0:
                sz = os.path.getsize(archive_path) if os.path.isfile(archive_path) else 0
                _snap_update(log=f'USB: zapisano {archive_name} ({sz / 1048576:.1f} MB)')
            else:
                _snap_update(log=f'USB: błąd zapisu — {r.stderr[:200]}')

        _snap_update(status='done', percent=100, message=f'Snapshot "{meta["label"]}" gotowy!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Błąd: {e}', log=f'WYJĄTEK: {e}')
        logger.exception("Snapshot creation failed")


# ── Snapshot restore worker ──

def _restore_snapshot_worker(snap_dir, restore_docker, restore_volumes,
                              restore_ethos, restore_system):
    """Background task: restores system from a snapshot."""
    try:
        meta_file = os.path.join(snap_dir, 'meta.json')
        if not os.path.isfile(meta_file):
            _snap_update(status='error', message='Brak meta.json w snapshocie')
            return

        with open(meta_file) as f:
            meta = json.load(f)

        _snap_update(percent=5, message='Przywracanie rozpoczęte...', log=f'Przywracanie: {meta.get("label", "?")}')

        steps_total = sum([restore_ethos, restore_system, restore_docker, restore_volumes])
        step_i = 0

        # ── 1. EthOS config & data ──
        if restore_ethos:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Przywracanie EthOS...', log='Przywracanie konfiguracji EthOS...')

            ethos_root = os.environ.get('ETHOS_ROOT', '/home/marcin/docker/nasos')
            ethos_bak = os.path.join(snap_dir, 'ethos')

            if os.path.isdir(ethos_bak):
                # ethos.env
                env_src = os.path.join(ethos_bak, 'ethos.env')
                if os.path.isfile(env_src):
                    shutil.copy2(env_src, os.path.join(ethos_root, 'ethos.env'))
                    _snap_update(log='  ethos.env przywrócony')

                # data/
                data_tar = os.path.join(ethos_bak, 'data.tar.gz')
                if os.path.isfile(data_tar):
                    # Backup current data before overwriting
                    data_current = os.path.join(ethos_root, 'data')
                    if os.path.isdir(data_current):
                        pre_restore = os.path.join(ethos_root, 'data.pre-restore')
                        if os.path.isdir(pre_restore):
                            shutil.rmtree(pre_restore, ignore_errors=True)
                        shutil.copytree(data_current, pre_restore)
                        _snap_update(log='  Obecne data/ skopiowane do data.pre-restore/')
                    subprocess.run(
                        ['tar', '-xzf', data_tar, '-C', ethos_root],
                        capture_output=True, timeout=120
                    )
                    _snap_update(log='  data/ przywrócone')

                # install.conf
                for extra in ['install.conf']:
                    src = os.path.join(ethos_bak, extra)
                    if os.path.isfile(src):
                        shutil.copy2(src, os.path.join(ethos_root, extra))

                _snap_update(log='EthOS — przywrócone')
            else:
                _snap_update(log='Brak danych EthOS w snapshocie')

        # ── 2. System configs ──
        if restore_system:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Przywracanie konfiguracji systemu...', log='Przywracanie systemu...')

            sys_bak = os.path.join(snap_dir, 'system')
            if os.path.isdir(sys_bak):
                # Hostname
                hostname_f = os.path.join(sys_bak, 'hostname')
                if os.path.isfile(hostname_f):
                    shutil.copy2(hostname_f, '/etc/hostname')
                    hostname = _read_file_safe(hostname_f, '').strip()
                    if hostname:
                        subprocess.run(['hostnamectl', 'set-hostname', hostname],
                                       capture_output=True, timeout=10)
                    _snap_update(log=f'  Hostname: {hostname}')

                # hosts
                hosts_f = os.path.join(sys_bak, 'hosts')
                if os.path.isfile(hosts_f):
                    shutil.copy2(hosts_f, '/etc/hosts')

                # Timezone
                tz_link = os.path.join(sys_bak, 'timezone_link')
                if os.path.isfile(tz_link):
                    tz_path = _read_file_safe(tz_link, '').strip()
                    if tz_path and os.path.isfile(tz_path):
                        try:
                            os.remove('/etc/localtime')
                        except Exception:
                            pass
                        os.symlink(tz_path, '/etc/localtime')
                        _snap_update(log=f'  Timezone: {tz_path}')

                # NetworkManager connections
                nm_tar = os.path.join(sys_bak, 'nm-connections.tar.gz')
                if os.path.isfile(nm_tar):
                    subprocess.run(
                        ['tar', '-xzf', nm_tar, '-C', '/etc/NetworkManager'],
                        capture_output=True, timeout=30
                    )
                    subprocess.run(['nmcli', 'connection', 'reload'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  NetworkManager connections przywrócone')

                # Nginx sites
                nginx_tar = os.path.join(sys_bak, 'nginx-sites.tar.gz')
                if os.path.isfile(nginx_tar):
                    subprocess.run(
                        ['tar', '-xzf', nginx_tar, '-C', '/etc/nginx'],
                        capture_output=True, timeout=30
                    )
                    subprocess.run(['systemctl', 'reload', 'nginx'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Nginx sites przywrócone')

                # Let's Encrypt
                le_tar = os.path.join(sys_bak, 'letsencrypt.tar.gz')
                if os.path.isfile(le_tar):
                    subprocess.run(
                        ['tar', '-xzf', le_tar, '-C', '/etc'],
                        capture_output=True, timeout=60
                    )
                    _snap_update(log='  Let\'s Encrypt certs przywrócone')

                # Samba
                smb_f = os.path.join(sys_bak, 'smb.conf')
                if os.path.isfile(smb_f):
                    shutil.copy2(smb_f, '/etc/samba/smb.conf')
                    subprocess.run(['systemctl', 'restart', 'smbd'],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Samba config przywrócony')

                # fstab (copy but don't apply — user must reboot)
                fstab_f = os.path.join(sys_bak, 'fstab')
                if os.path.isfile(fstab_f):
                    shutil.copy2(fstab_f, '/etc/fstab')
                    _snap_update(log='  fstab przywrócony (wymaga reboot)')

                # Crontab
                cron_f = os.path.join(sys_bak, 'crontab')
                if os.path.isfile(cron_f):
                    subprocess.run(['crontab', cron_f],
                                   capture_output=True, timeout=10)
                    _snap_update(log='  Crontab przywrócony')

                _snap_update(log='System config — przywrócone')
            else:
                _snap_update(log='Brak konfiguracji systemu w snapshocie')

        # ── 3. Docker compose projects ──
        if restore_docker:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Przywracanie Docker...', log='Przywracanie projektów Docker...')

            docker_bak = os.path.join(snap_dir, 'docker')
            proj_bak = os.path.join(docker_bak, 'projects')

            if os.path.isdir(proj_bak) and _docker_ok():
                compose_root = os.environ.get('COMPOSE_ROOT', '/home/marcin/docker')
                restored = 0

                for proj_name in sorted(os.listdir(proj_bak)):
                    proj_src = os.path.join(proj_bak, proj_name)
                    if not os.path.isdir(proj_src):
                        continue

                    proj_dest = os.path.join(compose_root, proj_name)
                    os.makedirs(proj_dest, exist_ok=True)

                    # Copy compose file + env files
                    for fn in os.listdir(proj_src):
                        src = os.path.join(proj_src, fn)
                        dst = os.path.join(proj_dest, fn)
                        if os.path.isfile(src):
                            shutil.copy2(src, dst)

                    # Pull images + start project
                    _snap_update(log=f'  Projekt {proj_name}: pobieranie obrazów...')
                    subprocess.run(
                        ['docker', 'compose', 'pull'],
                        capture_output=True, timeout=300, cwd=proj_dest
                    )
                    r = subprocess.run(
                        ['docker', 'compose', 'up', '-d'],
                        capture_output=True, text=True, timeout=120, cwd=proj_dest
                    )
                    if r.returncode == 0:
                        _snap_update(log=f'  Projekt {proj_name}: uruchomiony ✓')
                        restored += 1
                    else:
                        _snap_update(log=f'  Projekt {proj_name}: BŁĄD — {r.stderr[:200]}')

                _snap_update(log=f'Docker projects: {restored} przywróconych')
            else:
                _snap_update(log='Brak projektów Docker w snapshocie lub Docker niedostępny')

        # ── 4. Docker volumes ──
        if restore_volumes and restore_docker:
            step_i += 1
            pct = int(step_i / steps_total * 85)
            _snap_update(percent=pct, message='Przywracanie Docker volumes...', log='Import wolumenów Docker...')

            vol_bak = os.path.join(snap_dir, 'docker', 'volumes')
            if os.path.isdir(vol_bak) and _docker_ok():
                restored_vols = 0
                vol_archives = [f for f in os.listdir(vol_bak) if f.endswith('.tar.gz')]

                for vi, vf in enumerate(vol_archives):
                    vol_name = vf.replace('.tar.gz', '')
                    vol_pct = int(pct + (vi / max(len(vol_archives), 1)) * (85 - pct))
                    _snap_update(
                        percent=vol_pct,
                        message=f'Import: {vol_name}',
                        log=f'  Volume: {vol_name}'
                    )

                    # Create volume if needed
                    _docker_cmd(['volume', 'create', vol_name])

                    # Stop containers using this volume before restoring
                    containers_using = _containers_using_volume(vol_name)
                    stopped = []
                    for cid in containers_using:
                        _docker_cmd(['stop', cid], timeout=30)
                        stopped.append(cid)
                        _snap_update(log=f'    Zatrzymano kontener {cid[:12]}')

                    # Import volume data
                    archive_path = os.path.join(vol_bak, vf)
                    r = subprocess.run(
                        ['docker', 'run', '--rm',
                         '-v', f'{vol_name}:/volume_data',
                         '-v', f'{vol_bak}:/backup:ro',
                         'alpine', 'sh', '-c',
                         f'rm -rf /volume_data/* && tar -xzf /backup/{vf} -C /volume_data'],
                        capture_output=True, text=True, timeout=600
                    )
                    if r.returncode == 0:
                        restored_vols += 1
                    else:
                        _snap_update(log=f'    BŁĄD importu {vol_name}: {r.stderr[:200]}')

                    # Restart stopped containers
                    for cid in stopped:
                        _docker_cmd(['start', cid], timeout=30)
                        _snap_update(log=f'    Uruchomiono ponownie {cid[:12]}')

                _snap_update(log=f'Volumes: {restored_vols}/{len(vol_archives)} przywrócone')
            else:
                _snap_update(log='Brak volumes w snapshocie lub Docker niedostępny')

        _snap_update(status='done', percent=100,
                     message=f'Przywracanie "{meta.get("label", "")}" zakończone!')

    except Exception as e:
        _snap_update(status='error', percent=0, message=f'Błąd: {e}', log=f'WYJĄTEK: {e}')
        logger.exception("Snapshot restore failed")


# ── Helpers for snapshots ──

def _read_file_safe(path, default=''):
    try:
        with open(path, 'r') as f:
            return f.read()
    except Exception:
        return default


def _dir_size_str(path):
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for fn in files:
                total += os.path.getsize(os.path.join(root, fn))
    except Exception:
        pass
    if total > 1073741824:
        return f'{total / 1073741824:.1f} GB'
    if total > 1048576:
        return f'{total / 1048576:.1f} MB'
    return f'{total / 1024:.0f} KB'


def _find_env_files(compose_file):
    """Find extra .env files referenced in a compose file."""
    envs = []
    try:
        with open(compose_file, 'r') as f:
            content = f.read()
        for m in re.finditer(r'env_file:\s*\n\s*-\s*(.+)', content):
            fn = m.group(1).strip().strip('"').strip("'")
            if fn and fn != '.env':
                envs.append(fn)
        for m in re.finditer(r'env_file:\s+(.+)', content):
            fn = m.group(1).strip().strip('"').strip("'")
            if fn and fn != '.env' and not fn.startswith('-'):
                envs.append(fn)
    except Exception:
        pass
    return envs


def _containers_using_volume(vol_name):
    """Return list of container IDs using a given volume."""
    out, rc = _docker_cmd(['ps', '-a', '-q', '--filter', f'volume={vol_name}'])
    if rc == 0 and out:
        return [c.strip() for c in out.split('\n') if c.strip()]
    return []
