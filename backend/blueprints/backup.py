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

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from utils import get_ethos_user

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
from host import app_path, data_path, log_path, q, host_run
from crypto_utils import encrypt_secret, decrypt_secret

backup_bp = Blueprint('backup', __name__, url_prefix='/api/backup')

# Thread pool for filesystem calls that may hang on stale mounts
_fs_executor = ThreadPoolExecutor(max_workers=4)


# ── Encryption helpers ──

def encrypt_backup_gpg(file_path, passphrase):
    """Encrypt a backup file using GPG symmetric AES-256.
    Returns the path of the encrypted file (.gpg)."""
    encrypted_path = file_path + '.gpg'
    result = subprocess.run(
        ['gpg', '--symmetric', '--cipher-algo', 'AES256',
         '--batch', '--yes', '--passphrase-fd', '0',
         '--output', encrypted_path, file_path],
        input=passphrase,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise Exception(f"GPG encryption error: {result.stderr.strip()[:300]}")
    return encrypted_path


def decrypt_backup_gpg(encrypted_path, passphrase, output_path):
    """Decrypt a GPG-encrypted backup file.
    Returns output_path on success, raises on failure."""
    result = subprocess.run(
        ['gpg', '--decrypt', '--batch', '--yes', '--passphrase-fd', '0',
         '--output', output_path, encrypted_path],
        input=passphrase,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise Exception(f"GPG decryption error (wrong password?): {result.stderr.strip()[:300]}")
    return output_path


def _resolve_encryption_passphrase(enc):
    """Return the encryption passphrase for the given encryption config dict.

    For mode='key': decrypts and returns the stored key (scheduled backups work).
    For mode='passphrase': returns None — caller must supply the passphrase.
    Returns None if encryption is not enabled."""
    if not enc or not enc.get('enabled'):
        return None
    if enc.get('mode') == 'key':
        stored = enc.get('stored_key')
        if not stored:
            raise Exception("Encryption key was not generated for this profile.")
        return decrypt_secret(stored)
    return None  # passphrase mode — must be provided by caller


def _prepare_encryption(enc_input, existing_enc=None):
    """Normalise encryption config from API input.

    - Preserves stored_key when mode='key' and client didn't send one.
    - Generates a new stored_key when switching to mode='key' for the first time.
    Returns (enc_dict_to_store, plaintext_key_for_user_or_None).
    """
    if not enc_input or not enc_input.get('enabled'):
        return None, None

    enc = dict(enc_input)
    enc.setdefault('mode', 'passphrase')
    generated_key = None

    if enc['mode'] == 'key':
        if enc.get('stored_key'):
            pass  # Client re-sent an (unusable) stored_key — ignore, use existing
        if existing_enc and existing_enc.get('mode') == 'key' and existing_enc.get('stored_key'):
            enc['stored_key'] = existing_enc['stored_key']
        else:
            # First time switching to key mode — generate a fresh key
            generated_key = secrets.token_hex(32)
            enc['stored_key'] = encrypt_secret(generated_key)
    else:
        enc.pop('stored_key', None)

    return enc, generated_key

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

_snapshot_state = {
    'status': 'idle',   # idle | creating | restoring | done | error
    'percent': 0,
    'message': '',
    'log': [],
    '_started': 0,      # timestamp when operation started (for stuck detection)
}

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
    """Return live in-progress backup notification for the global panel."""
    notifs = []
    with operation_lock:
        op = current_operation
    with _progress_lock:
        prog = progress_state.get('last')
        prog_ts = progress_state.get('timestamp', time.time())
    if op and prog:
        pct = prog.get('overall_percent', prog.get('percent', 0))
        stage = prog.get('stage', 'archive')
        stage_label = 'Transfer' if stage == 'transfer' else 'Archiving'
        notifs.append({
            'type': 'progress',
            'title': 'Backup w toku',
            'message': f'{stage_label}: {round(pct)}%',
            'time': prog_ts,
            'action': {'app': 'backup', 'tab': 'backup'}
        })
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
        enc_raw = None
        try:
            enc_raw = row['encryption']
        except Exception:
            pass
        enc = None
        if enc_raw:
            try:
                enc = json.loads(enc_raw)
            except Exception:
                pass
        # Strip stored_key before returning to clients (sensitive — never expose)
        enc_public = None
        if enc:
            enc_public = {k: v for k, v in enc.items() if k != 'stored_key'}
        profiles.append({
            'id': str(row['id']),
            'name': row['name'],
            'paths': json.loads(row['paths']),
            'destination': json.loads(row['destination']) if row['destination'] else None,
            'schedule': sched,
            'options': row['options'],
            'retention': row['retention'] if row['retention'] else 0,
            'incremental': bool(row['incremental']) if row['incremental'] else False,
            'encryption': enc_public,
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
        emit_log(f"Copying to SSH: {ssh_config['host']}", 'info')
        source_size = os.path.getsize(backup_path)

        def progress_callback(filename, size, sent):
            percent = (sent / size * 100) if size > 0 else 0
            emit_progress(
                operation=f"Copying to SSH: {ssh_config['host']}",
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
        ssh.exec_command(f"mkdir -p {q(remote_path)}")
        with SCPClient(ssh.get_transport(), progress=progress_callback) as scp:
            scp.put(backup_path, remote_file)
        ssh.close()
        emit_log(f"Skopiowano do SSH: {remote_file}", 'success')
        return True, remote_file
    except Exception as e:
        return False, str(e)


# ── Retention ──

def _is_backup_file(f):
    return f.startswith('backup_') and (f.endswith('.tar.gz') or f.endswith('.tar.gz.gpg'))


def apply_retention(retention, backup_dir, destination=None, profile_name=None):
    if not retention or retention <= 0:
        return
    try:
        local_backups = sorted(
            [f for f in os.listdir(backup_dir) if _is_backup_file(f)],
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
                    emit_log(f"Rotation: removed {f}", 'info')
                except Exception as e:
                    emit_log(f"Rotation: deletion error {f}: {e}", 'warning')

        if destination and destination.get('type') == 'usb' and destination.get('path'):
            usb_path = destination['path']
            if os.path.isdir(usb_path):
                usb_backups = sorted(
                    [f for f in os.listdir(usb_path) if _is_backup_file(f)],
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
        emit_log(f"Rotation error: {e}", 'warning')


# ── Incremental chain detection ──

def detect_incremental_chain(selected_file, search_dir=None):
    if '_incr' not in selected_file:
        return [selected_file]
    d = search_dir or BACKUP_DIR
    all_backups = sorted(
        [f for f in os.listdir(d) if _is_backup_file(f)]
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

def run_backup(paths, destination=None, profile_name=None, retention=0, incremental=False, encrypt_passphrase=None):
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
        mode_label = "incremental" if incremental else "full"
        emit_log(f"Backup {mode_label} — {len(paths)} paths...", 'info')

        # Validate paths before starting
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            raise Exception(f"Source paths do not exist: {', '.join(missing)}")

        if direct_to_usb:
            dest_dir = destination['path']
            if not os.path.isdir(dest_dir):
                raise Exception(f"Destination directory does not exist: {dest_dir}")

        total_bytes = 0
        total_files = 0
        for path in paths:
            if os.path.exists(path):
                total_bytes += get_size(path, exclude=True)
                total_files += count_files(path, exclude=True)
        history_entry['files_count'] = total_files
        emit_log(f"Size: {total_bytes / (1024**3):.2f} GB, files: {total_files}", 'info')

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
                            _is_backup_file(f) and '_incr' not in f
                            for f in os.listdir(dest_dir)
                        )
                    except Exception:
                        pass
                # Only check local backup dir if there is NO destination configured
                if not has_full_backup and not dest_dir:
                    try:
                        has_full_backup = any(
                            _is_backup_file(f) and '_incr' not in f
                            for f in os.listdir(BACKUP_DIR)
                        )
                    except Exception:
                        pass

                if not has_full_backup:
                    os.remove(snapshot_path)
                    emit_log("No full base backup — resetting snapshot and creating new full backup", 'warning')

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
            emit_log(f"Archive being created directly on USB: {usb_dir}", 'info')
        else:
            backup_path = os.path.join(BACKUP_DIR, backup_filename)
        history_entry['archive_file'] = backup_filename

        cmd = ['tar', '-cvzf', backup_path]
        if snapshot_path is not None:
            cmd.append(f'--listed-incremental={snapshot_path}')
            if is_true_incremental:
                emit_log("Incremental backup — archiving only changed files", 'info')
            else:
                emit_log("First backup — creating full archive with snapshot file", 'info')

        for path in paths:
            if os.path.exists(path):
                stripped = path.lstrip('/')
                for excl in ['backups', 'Backups', 'backup', '.cache', 'cache']:
                    cmd.append(f'--exclude={stripped}/{excl}')
                    cmd.append(f'--exclude={stripped}/*/{excl}')

        for path in paths:
            if os.path.exists(path):
                cmd.extend(['-C', '/', path.lstrip('/')])

        emit_log("Step 1: Archiving...", 'info')
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        files_done = 0
        bytes_done = 0
        archive_start = time.time()
        last_progress_time = 0
        tar_errors = []

        for line in iter(process.stdout.readline, ''):
            line = line.strip()
            if line:
                files_done += 1
                try:
                    if os.path.exists('/' + line):
                        bytes_done += os.path.getsize('/' + line)
                except Exception:
                    pass
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
                        operation=f"Archiving: {backup_filename}",
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
            err_detail = stderr_output.strip()[:500] if stderr_output.strip() else 'no details'
            emit_log(f"tar stderr: {err_detail}", 'error')
            raise Exception(f"Archive error (code: {process.returncode}): {err_detail}")
        elif process.returncode == 1:
            if stderr_output.strip():
                emit_log(f"tar warnings: {stderr_output.strip()[:300]}", 'warning')
            emit_log("Archive completed with warnings", 'warning')

        final_size = os.path.getsize(backup_path) if os.path.exists(backup_path) else 0
        history_entry['size'] = final_size
        emit_log(f"Archiwum: {backup_filename} ({final_size / (1024*1024):.2f} MB)", 'success')

        # ── Encryption step ──
        if encrypt_passphrase:
            emit_log("Step: Encrypting archive (AES-256)...", 'info')
            encrypted_path = encrypt_backup_gpg(backup_path, encrypt_passphrase)
            os.remove(backup_path)
            backup_path = encrypted_path
            backup_filename = backup_filename + '.gpg'
            history_entry['archive_file'] = backup_filename
            enc_size = os.path.getsize(backup_path)
            history_entry['size'] = enc_size
            history_entry['encrypted'] = True
            emit_log(f"Encrypted: {backup_filename} ({enc_size / (1024*1024):.2f} MB)", 'success')

        final_location = backup_path
        if destination:
            if destination['type'] == 'usb':
                # Archive already created directly on USB — no transfer needed
                final_location = backup_path
                emit_log(f"Archive saved to USB: {backup_path}", 'success')
            elif destination['type'] == 'ssh':
                emit_log("Step 2: Copying to SSH...", 'info')
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
        emit_log(f"Backup completed: {final_location}", 'success')
        _emit('backup_complete', {'message': f'Backup completed: {backup_filename}'})
        # Persistent notification — stays until user clears
        try:
            from app import add_persistent_notification
            size_mb = round(history_entry.get('size', 0) / (1024 * 1024), 1)
            add_persistent_notification(
                'Backup completed',
                f'{backup_filename} — {size_mb} MB, {round(history_entry["duration"])}s',
                ntype='success',
                action={'app': 'backup', 'tab': 'history'}
            )
        except Exception:
            pass
        if retention and retention > 0:
            # For USB: apply retention on USB directory (archives are there)
            retention_dir = destination['path'] if direct_to_usb else BACKUP_DIR
            apply_retention(retention, retention_dir, destination, profile_name)

        # If transferred to SSH, remove ALL local copies (no local retention)
        # For USB: archive was created directly there, no local copies to clean
        if has_transfer:
            try:
                for f in os.listdir(BACKUP_DIR):
                    if f.startswith('backup_') and (f.endswith('.tar.gz') or f.endswith('.tar.gz.gpg')):
                        try:
                            os.remove(os.path.join(BACKUP_DIR, f))
                        except Exception:
                            pass
                emit_log("Local copies removed (backup on external drive)", 'info')
            except Exception:
                pass

    except Exception as e:
        history_entry['status'] = 'failed'
        history_entry['error'] = str(e)
        history_entry['duration'] = round(time.time() - start_time, 1)
        add_to_history(history_entry)
        with _progress_lock:
            progress_state['last'] = None
        emit_log(f"Backup error: {e}", 'error')
        _emit('backup_error', {'message': str(e)})
        try:
            from app import add_persistent_notification
            add_persistent_notification(
                'Backup failed',
                str(e)[:200],
                ntype='error',
                action={'app': 'backup', 'tab': 'history'}
            )
        except Exception:
            pass
    finally:
        with operation_lock:
            current_operation = None


# ── Restore ──

def run_restore(backup_file, target_path=None, archive_dir=None, decrypt_passphrase=None):
    global current_operation
    d = archive_dir or BACKUP_DIR
    temp_decrypted_files = []
    try:
        # ── Decryption step for encrypted backups ──
        if backup_file.endswith('.tar.gz.gpg'):
            if not decrypt_passphrase:
                raise Exception("Backup is encrypted. Enter the decryption password.")
            decrypted_name = backup_file[:-4]  # strip .gpg
            decrypted_path = os.path.join(d, decrypted_name)
            temp_decrypted_files.append(decrypted_path)
            emit_log("Decrypting archive (AES-256)...", 'info')
            decrypt_backup_gpg(os.path.join(d, backup_file), decrypt_passphrase, decrypted_path)
            emit_log("Decrypted successfully", 'success')
            backup_file = decrypted_name

        chain = detect_incremental_chain(backup_file, search_dir=d)

        # ── Decrypt any remaining encrypted chain members (e.g. full base of an incremental chain) ──
        for i, cf in enumerate(chain):
            if cf.endswith('.tar.gz.gpg'):
                if not decrypt_passphrase:
                    raise Exception(f"Chain file {cf} is encrypted — enter password.")
                cf_decrypted_name = cf[:-4]
                cf_decrypted_path = os.path.join(d, cf_decrypted_name)
                emit_log(f"Decrypting: {cf}...", 'info')
                decrypt_backup_gpg(os.path.join(d, cf), decrypt_passphrase, cf_decrypted_path)
                temp_decrypted_files.append(cf_decrypted_path)
                chain[i] = cf_decrypted_name
        extract_to = target_path if target_path else '/'
        if target_path:
            os.makedirs(target_path, exist_ok=True)

        if len(chain) > 1:
            emit_log(f"Incremental chain — {len(chain)} archives", 'info')
        else:
            emit_log(f"Restoring from {backup_file}...", 'info')

        if target_path:
            emit_log(f"Target: {target_path}", 'info')

        total_files = 0
        total_bytes = 0
        for f in chain:
            fpath = os.path.join(d, f)
            if not os.path.exists(fpath):
                raise Exception(f"File not found: {f}")
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
            for line in iter(process.stdout.readline, ''):
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
                            operation=f"Restoring: {archive_file}",
                            percent=percent, current_file=line[-60:] if len(line) > 60 else line,
                            files_done=files_done, total_files=total_files,
                            bytes_done=int(total_bytes * percent / 100),
                            total_bytes=total_bytes, eta=eta
                        )

            process.wait()
            if process.returncode >= 2:
                raise Exception(f"Extraction error for {archive_file} (code: {process.returncode})")
            elif process.returncode == 1:
                emit_log(f"Warnings during extraction of {archive_file}", 'warning')

        emit_log(f"Restore completed successfully! → {target_path or 'original locations'}", 'success')
        with _progress_lock:
            progress_state['last'] = None
        _emit('backup_complete', {'message': f'Restore completed: {backup_file}'})
        try:
            from app import add_persistent_notification
            add_persistent_notification(
                'Restore completed',
                f'{backup_file} → {target_path or "original locations"}',
                ntype='success',
                action={'app': 'backup', 'tab': 'history'}
            )
        except Exception:
            pass

    except Exception as e:
        emit_log(f"Restore error: {e}", 'error')
        with _progress_lock:
            progress_state['last'] = None
        _emit('backup_error', {'message': str(e)})
        try:
            from app import add_persistent_notification
            add_persistent_notification(
                'Restore failed',
                str(e)[:200],
                ntype='error',
                action={'app': 'backup', 'tab': 'history'}
            )
        except Exception:
            pass
    finally:
        # Clean up all temporary decrypted files (sensitive data)
        for tmp in temp_decrypted_files:
            if tmp and os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
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
    encryption = profile.get('encryption')

    # Resolve encryption passphrase for this scheduled run
    encrypt_passphrase = None
    if encryption and encryption.get('enabled'):
        enc_mode = encryption.get('mode', 'passphrase')
        if enc_mode == 'key':
            # Key mode: load stored_key from DB (load_profiles strips it for security)
            try:
                conn = get_db_connection()
                row = conn.execute('SELECT encryption FROM profiles WHERE id = ?', (profile['id'],)).fetchone()
                conn.close()
                if row and row['encryption']:
                    enc_full = json.loads(row['encryption'])
                    encrypt_passphrase = _resolve_encryption_passphrase(enc_full)
            except Exception as e:
                log_scheduler(f"BACKUP FAILED - profile '{profile_name}': encryption key error: {e}", 'ERROR')
                with operation_lock:
                    if current_operation == 'backup':
                        current_operation = None
                return
        else:
            log_scheduler(f"BACKUP SKIPPED - profile '{profile_name}' requires passphrase encryption — scheduled backups do not support passphrase encryption. Use 'Auto key' mode.", 'WARNING')
            emit_log(f"Scheduled backup '{profile_name}' skipped — profile uses passphrase encryption. Switch to 'Auto key' or run manually.", 'warning')
            with operation_lock:
                if current_operation == 'backup':
                    current_operation = None
            return

    start = time.time()
    try:
        run_backup(profile['paths'], destination, profile_name, retention, incremental, encrypt_passphrase)
        duration = round(time.time() - start, 1)
        log_scheduler(f"BACKUP COMPLETED - profile '{profile_name}' in {duration}s")
    except Exception as e:
        duration = round(time.time() - start, 1)
        log_scheduler(f"BACKUP FAILED - profile '{profile_name}' after {duration}s: {e}", 'ERROR')


# ═══════════════════════════════════════════════════════════
#  API Routes
# ═══════════════════════════════════════════════════════════

@backup_bp.route('/backup', methods=['POST'])
def start_backup():
    global current_operation
    with operation_lock:
        if current_operation is not None:
            return jsonify({'error': 'Inna operacja jest w toku'}), 400
        current_operation = 'backup'

    data = request.json or {}
    paths = data.get('paths', [])
    destination = data.get('destination')
    retention = data.get('retention', 0)
    incremental = data.get('incremental', False)
    encrypt_passphrase = data.get('encrypt_passphrase') or None

    if not paths:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'Select at least one path'}), 400

    valid_paths = [p for p in paths if os.path.exists(p)]
    if not valid_paths:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'No paths exist'}), 400

    if destination:
        if destination['type'] == 'usb':
            if not os.path.exists(destination.get('path', '')):
                with operation_lock:
                    current_operation = None
                return jsonify({'error': 'USB is not available'}), 400
        elif destination['type'] == 'ssh':
            ssh_id = destination.get('server_id')
            if ssh_id:
                configs = load_ssh_configs()
                ssh_cfg = next((c for c in configs if c.get('id') == ssh_id), None)
                if not ssh_cfg:
                    with operation_lock:
                        current_operation = None
                    return jsonify({'error': 'SSH server not found'}), 400
                destination['config'] = ssh_cfg

    _socketio.start_background_task(run_backup, valid_paths, destination, retention=retention, incremental=incremental, encrypt_passphrase=encrypt_passphrase)
    return jsonify({'status': 'ok'})


@backup_bp.route('/backup-preview/<filename>')
def backup_preview(filename):
    custom_path = request.args.get('path')
    if custom_path and os.path.isfile(custom_path) and (custom_path.endswith('.tar.gz') or custom_path.endswith('.tar.gz.gpg')):
        bp = custom_path
        archive_dir = os.path.dirname(custom_path)
    else:
        bp = os.path.join(BACKUP_DIR, filename)
        archive_dir = BACKUP_DIR
    if not os.path.exists(bp):
        return jsonify({'error': 'File does not exist'}), 404
    st = os.stat(bp)
    # Encrypted backups cannot be previewed without passphrase
    if filename.endswith('.tar.gz.gpg') or bp.endswith('.tar.gz.gpg'):
        return jsonify({
            'filename': filename, 'size': st.st_size,
            'modified': datetime.fromtimestamp(st.st_mtime).isoformat(),
            'total_files': 0, 'is_incremental': '_incr' in filename,
            'encrypted': True, 'chain': [], 'top_dirs': {},
            'files': [], 'truncated': False
        })
    result = subprocess.run(['tar', '-tzf', bp], capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return jsonify({'error': 'Cannot read archive'}), 500
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

    data = request.json or {}
    backup_file = data.get('backup_file', '')
    backup_path = data.get('backup_path', '')
    target_path = data.get('target_path', '')
    decrypt_passphrase = data.get('decrypt_passphrase') or None
    if not backup_file:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'Wybierz plik backupu'}), 400
    # Resolve archive location: prefer explicit path, fallback to BACKUP_DIR
    if backup_path and os.path.isfile(backup_path) and (backup_path.endswith('.tar.gz') or backup_path.endswith('.tar.gz.gpg')):
        bp = backup_path
        archive_dir = os.path.dirname(backup_path)
    else:
        bp = os.path.join(BACKUP_DIR, backup_file)
        archive_dir = BACKUP_DIR
    if not os.path.exists(bp):
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'File does not exist'}), 400
    # Validate passphrase is provided for encrypted backups
    if backup_file.endswith('.tar.gz.gpg') and not decrypt_passphrase:
        with operation_lock:
            current_operation = None
        return jsonify({'error': 'Backup is encrypted — enter decryption password', 'encrypted': True}), 400
    restore_target = target_path or None
    if restore_target:
        try:
            os.makedirs(restore_target, exist_ok=True)
        except Exception as e:
            with operation_lock:
                current_operation = None
            return jsonify({'error': f'Cannot create directory: {e}'}), 400

    _socketio.start_background_task(run_restore, backup_file, restore_target, archive_dir, decrypt_passphrase)
    return jsonify({'status': 'ok'})


@backup_bp.route('/status')
def get_status():
    with operation_lock:
        return jsonify({'busy': current_operation is not None, 'operation': current_operation})

@backup_bp.route('/progress')
def get_progress():
    with _progress_lock:
        return jsonify({'progress': progress_state.get('last')})


@backup_bp.route('/tasks')
def tasks_compat():
    """Compatibility alias — frontend historically used /api/backup/tasks."""
    # Return status + scheduling info as a combined "tasks" view
    try:
        from blueprints.backup_scheduling import get_schedule_list
        schedule = get_schedule_list()
    except Exception:
        schedule = []
    with operation_lock:
        busy = current_operation is not None
        op = current_operation
    return jsonify({
        'busy': busy,
        'current_operation': op,
        'scheduled_tasks': schedule
    })


# ── Sub-module route registration ──
from blueprints import backup_config, backup_scheduling, backup_history, backup_snapshots
